"""
Various helper utilities necessary for clients of the topology
service.
"""

import os
import sys
import urllib3
import fnmatch
import urllib.parse as urlparse
from getpass import getpass
import xml.etree.ElementTree as ET

import requests

# List of contact types stored in Topology data
# At time of writing, there isn't anything that restricts a contact to one of these types
CONTACT_TYPES = ["administrative",
                 "miscellaneous",
                 "security",
                 "submitter",
                 "site",
                 "local executive",
                 "local operational",
                 "local security"]

class Error(Exception):
    pass

class AuthError(Error):
    pass

class InvalidPathError(AuthError):
    pass

class IncorrectPasswordError(AuthError):
    pass


def update_url_hostname(url, args):
    """
    Given a URL and an argument object, update the URL's hostname
    according to args.host and return the newly-formed URL.
    """
    if not args.host:
        return url
    url_list = list(urlparse.urlsplit(url))
    url_list[1] = args.host
    return urlparse.urlunsplit(url_list)


def get_contact_list_info(contact_list):
    """
    Get contact list info out of contact list

    In rgsummary, this looks like:
        <ContactLists>
            <ContactList>
                <ContactType>Administrative Contact</ContactType>
                <Contacts>
                    <Contact>
                        <Name>Matyas Selmeci</Name>
                        ...
                    </Contact>
                </Contacts>
            </ContactList>
            ...
        </ContactLists>

    and the arg `contact_list` is the contents of a single <ContactList>

    If vosummary, this looks like:
        <ContactTypes>
            <ContactType>
                <Type>Miscellaneous Contact</Type>
                <Contacts>
                    <Contact>
                        <Name>...</Name>
                        ...
                    </Contact>
                    ...
                </Contacts>
            </ContactType>
            ...
        </ContactTypes>

    and the arg `contact_list` is the contents of <ContactTypes>


    Returns: a list of dicts that each look like:
    { 'ContactType': 'Administrative Contact',
    'Name': 'Matyas Selmeci',
    'Email': '...',
    ...
    }
    """
    contact_list_info = []
    for contact in contact_list:
        if contact.tag == 'ContactType' or contact.tag == 'Type':
            contact_list_type = contact.text.lower()
        if contact.tag == 'Contacts':
            for con in contact:
                contact_info = { 'ContactType' : contact_list_type }
                for contact_contents in con:
                    contact_info[contact_contents.tag] = contact_contents.text
                contact_list_info.append(contact_info)

    return contact_list_info


def filter_contacts(args, results):
    """
    Given a set of result contacts, filter them according to given arguments
    """
    results = dict(results)  # make a copy so we don't modify the original

    if getattr(args, 'name_filter', None):
        # filter out undesired names
        for name in list(results):
            if not fnmatch.fnmatch(name, args.name_filter) and \
                    args.name_filter not in name:
                del results[name]
    elif getattr(args, 'fqdn_filter', None):
        # filter out undesired FQDNs
        for fqdn in list(results):
            if not fnmatch.fnmatch(fqdn, args.fqdn_filter) and \
                    args.fqdn_filter not in fqdn:
                del results[fqdn]

    if 'all' not in args.contact_type:
        # filter out undesired contact types
        for name in list(results):
            contact_list = []
            for contact in results[name]:
                contact_type = contact['ContactType']
                for args_contact_type in args.contact_type:
                    if contact_type.startswith(args_contact_type):
                        contact_list.append(contact)
            if contact_list == []:
                del results[name]
            else:
                results[name] = contact_list

    if getattr(args, 'contact_emails', None):
        for name in list(results):
            contact_list = [contact for contact in results[name] if contact['Email'] in args.contact_emails]
            if not contact_list:
                del results[name]
            else:
                results[name] = contact_list

    return results


class TopologyPoolManager(urllib3.PoolManager):

    def __init__(self):
        self.session = False
        super().__init__()

    def get_auth_session(self, args):
        """
        Return a requests session ready for an XML query.
        """
        euid = os.geteuid()
        if euid == 0:
            cert = '/etc/grid-security/hostcert.pem'
            key = '/etc/grid-security/hostkey.pem'
        else:
            cert = f'/tmp/x509up_u{euid}'
            key = f'/tmp/x509up_u{euid}'

        cert = os.environ.get('X509_USER_PROXY', cert)
        key = os.environ.get('X509_USER_PROXY', key)

        if args.cert:
            cert = args.cert
        if args.key:
            key = args.key

        session = {}
        if os.path.exists(cert):
            session["cert_file"] = cert
        else:
            raise InvalidPathError(f"Error: could not find cert at {cert}")

        if os.path.exists(key):
            session["key_file"] = key
        else:
            raise InvalidPathError(f"Error: could not find key at {key}")

        session['cert_reqs'] = 'CERT_REQUIRED'
        session['key_password'] = getpass("decryption password: ")
        super().__dict__.update(**session)
        return True

    def get_vo_map(self,args):
        """
        Generate a dictionary mapping from the VO name (key) to the
        VO ID (value).
        """
        old_no_proxy = os.environ.pop('no_proxy', None)
        os.environ['no_proxy'] = '.opensciencegrid.org'

        url = update_url_hostname("https://topology.opensciencegrid.org/vosummary"
                                "/xml?all_vos=on&active_value=1", args)
        if not self.session:
            self.session = self.get_auth_session(args)
            response = self.request('GET',url)
        else:
            response = self.request('GET',url)

        if old_no_proxy is not None:
            os.environ['no_proxy'] = old_no_proxy
        else:
            del os.environ['no_proxy']

        if response.status_code != requests.codes.ok:
            raise Exception("MyOSG request failed (status %d): %s" % \
                (response.status_code, response.text[:2048]))

        root = ET.fromstring(response.content)
        if root.tag != 'VOSummary':
            raise Exception("MyOSG returned invalid XML with root tag %s" % root.tag)
        vo_map = {}
        for child_vo in root:
            if child_vo.tag != "VO":
                raise Exception("MyOSG returned a non-VO  (%s) inside VO summary." % \
                                root.tag)
            vo_info = {}
            for child_info in child_vo:
                vo_info[child_info.tag] = child_info.text
            if 'ID' in vo_info and 'Name' in vo_info:
                vo_map[vo_info['Name'].lower()] = vo_info['ID']

        return vo_map


    SERVICE_IDS = {'ce': 1,
                'srmv2': 3,
                'gridftp': 5,
                'xrootd': 142,
                'perfsonar-bandwidth': 130,
                'perfsonar-latency': 130,
                'gums': 101,
                }
    def mangle_url(self,url, args):
        """
        Given a MyOSG URL, switch to using the hostname specified in the
        arguments
        """
        if not args.host:
            return url
        url_list = list(urlparse.urlsplit(url))
        url_list[1] = args.host

        qs_dict = urlparse.parse_qs(url_list[3])
        qs_list = urlparse.parse_qsl(url_list[3])

        if getattr(args, 'provides_service', None):
            if 'service' not in qs_dict:
                qs_list.append(("service", "on"))
            for service in args.provides_service.split(","):
                service = service.strip().lower()
                service_id = self.SERVICE_IDS.get(service)
                if not service_id:
                    raise Exception("Requested service %s not known; known service"
                                    " names: %s" % (service, ", ".join(self.SERVICE_IDS)))
                qs_list.append(("service_sel[]", str(service_id)))

        if getattr(args, 'owner_vo', None):
            vo_map = self.get_vo_map(args)
            if 'voown' not in qs_dict:
                qs_list.append(("voown", "on"))
            for vo in args.owner_vo.split(","):
                vo = vo.strip().lower()
                vo_id = vo_map.get(vo)
                if not vo_id:
                    raise Exception("Requested owner VO %s not known; known VOs: %s" \
                        % (vo, ", ".join(vo_map)))
                qs_list.append(("voown_sel[]", str(vo_id)))

        url_list[3] = urlparse.urlencode(qs_list, doseq=True)

        return urlparse.urlunsplit(url_list)


    def get_contacts(self, args, urltype, roottype):
        """
        Get one type of contacts for OSG.
        """
        old_no_proxy = os.environ.pop('no_proxy', None)
        os.environ['no_proxy'] = '.opensciencegrid.org'

        base_url = "https://topology.opensciencegrid.org/" + urltype + "summary/xml?" \
                "&active=on&active_value=1&disable=on&disable_value=0"
        if(not self.session):
            self.session = self.get_auth_session(args)
        url = self.mangle_url(base_url, args)
        try:
            response = self.request('GET',url)
        except requests.exceptions.ConnectionError as exc:
            print(exc)
            try:
                if exc.args[0].args[1].errno == 22:
                    raise IncorrectPasswordError("Incorrect password, please try again")
                else:
                    raise exc
            except (TypeError, AttributeError, IndexError):
                print(exc)
                raise exc

        if old_no_proxy is not None:
            os.environ['no_proxy'] = old_no_proxy
        else:
            del os.environ['no_proxy']

        if response.status != requests.codes.ok:
            print("MyOSG request failed (status %d): %s" % \
                (response.status_code, response.text[:2048]), file=sys.stderr)
            return None

        root = ET.fromstring(response.data)
        if root.tag != roottype + 'Summary':
            print("MyOSG returned invalid XML with root tag %s" % root.tag,
                file=sys.stderr)
            return None

        return root


    def get_vo_contacts(self,args):
        """
        Get resource contacts for OSG.  Return results.
        """
        root = self.get_contacts(args, 'vo', 'VO')
        if root is None:
            return 1

        results = {}
        for child_vo in root:
            if child_vo.tag != "VO":
                print("MyOSG returned a non-VO (%s) inside summary." % \
                    root.tag, file=sys.stderr)
                return 1
            name = None
            contact_list_info = []
            for item in child_vo:
                if item.tag == 'Name':
                    name = item.text
                if item.tag == "ContactTypes":
                    for contact_type in item:
                        contact_list_info.extend( \
                            get_contact_list_info(contact_type))

            if name and contact_list_info:
                results[name] = contact_list_info

        return results


    def get_resource_contacts_by_name_and_fqdn(self,args):
        """
        Get resource contacts for OSG.  Return results.

        Returns two dictionaries, one keyed on the resource name and one keyed on
        the resource FQDN.
        """
        root = self.get_contacts(args, 'rg', 'Resource')
        if root is None:
            return {}, {}

        results_by_name = {}
        results_by_fqdn = {}
        for child_rg in root:
            if child_rg.tag != "ResourceGroup":
                print("MyOSG returned a non-resource group (%s) inside summary." % \
                    root.tag, file=sys.stderr)
                return {}, {}
            for child_res in child_rg:
                if child_res.tag != "Resources":
                    continue
                for resource in child_res:
                    resource_name = None
                    resource_fqdn = None
                    contact_list_info = []
                    for resource_tag in resource:
                        if resource_tag.tag == 'Name':
                            resource_name = resource_tag.text
                        if resource_tag.tag == 'FQDN':
                            resource_fqdn = resource_tag.text
                        if resource_tag.tag == 'ContactLists':
                            for contact_list in resource_tag:
                                if contact_list.tag == 'ContactList':
                                    contact_list_info.extend( \
                                        get_contact_list_info(contact_list))

                    if contact_list_info:
                        if resource_name:
                            results_by_name[resource_name] = contact_list_info
                        if resource_fqdn:
                            results_by_fqdn[resource_fqdn] = contact_list_info

        return results_by_name, results_by_fqdn


    def get_resource_contacts(self,args):
        return self.get_resource_contacts_by_name_and_fqdn(args)[0]


    def get_resource_contacts_by_fqdn(self, args):
        return self.get_resource_contacts_by_name_and_fqdn(args)[1]
