"""DNS Authenticator for the SchlundTech XML Gateway."""
import logging
import xml.etree.ElementTree as Et
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

import zope.interface
from certbot import errors
from certbot import interfaces
from certbot.plugins import dns_common

logger = logging.getLogger(__name__)

GATEWAY_URL = 'https://gateway.schlundtech.de'


@zope.interface.implementer(interfaces.IAuthenticator)
@zope.interface.provider(interfaces.IPluginFactory)
class Authenticator(dns_common.DNSAuthenticator):
    """DNS Authenticator for the SchlundTech XML Gateway.

    This Authenticator uses the SchlundTech XML Gateway API to fulfill a dns-01 challenge.
    """
    description = 'Obtain certificates using a DNS TXT record.'
    ttl = 60

    def __init__(self, *args, **kwargs):
        super(Authenticator, self).__init__(*args, **kwargs)
        self.credentials = None

    @classmethod
    def add_parser_arguments(cls, add):  # pylint: disable=arguments-differ
        super(Authenticator, cls).add_parser_arguments(add, default_propagation_seconds=30)
        add('credentials', help='SchlundTech XML Gateway credentials file.')

    def more_info(self):  # pylint: disable=missing-docstring,no-self-use
        return 'This plugin configures a DNS TXT record to respond to a dns-01 challenge using ' + \
               'the SchlundTech XML Gateway API.'

    def _setup_credentials(self):
        self.credentials = self._configure_credentials(
            'credentials',
            'SchlundTech XML Gateway credentials file',
            {
                'user': 'Username for the SchlundTech XML Gateway.',
                'password': 'Password for the SchlundTech XML Gateway.',
                'context': 'Context to use.'
            }
        )

    def _perform(self, domain, validation_name, validation):
        self._get_gateway_client().add_txt_record(domain, validation_name, validation)

    def _cleanup(self, domain, validation_name, validation):
        self._get_gateway_client().del_txt_record(domain, validation_name, validation)

    def _get_gateway_client(self):
        return _SchlundtechGatewayClient(
            user=self.credentials.conf('user'),
            password=self.credentials.conf('password'),
            context=self.credentials.conf('context'),
            ttl=self.ttl
        )


class _SchlundtechGatewayClient:
    """
    Encapsulates all communication with the SchlundTech XML Gateway.
    """
    def __init__(self, user, password, context, ttl):
        self.user = user
        self.password = password
        self.context = context
        self.ttl = ttl
        self._xml = _XML()

    def _auth(self):
        return {
            'user': self.user,
            'password': self.password,
            'context': self.context
        }

    def _call(self, task):
        request = {
            'auth': self._auth(),
            'language': 'en',
            'task': task
        }
        try:
            connection = urlopen(Request(
                url=GATEWAY_URL,
                data=self._xml.tostring('request', request),
                method='POST'
            ))
            response = self._xml.fromstring(connection.read())
            if response is not None and response['result'] is not None:
                return response['result']
            else:
                self._log_call_error(request, response, None)
                msg = ('\n' + repr(response)) if response is not None else ''
                raise errors.PluginError(
                    'Unexpected response received from {0}{1}'.format(GATEWAY_URL, msg)
                )
        except HTTPError as e:
            self._log_call_error(request, None, e)
            raise errors.PluginError(
                'Communication error while calling {0}\nResponse: {1}, {2}'.format(GATEWAY_URL, e.code, e.reason)
            )
        except URLError as e:
            self._log_call_error(request, None, e)
            raise errors.PluginError(
                'Communication error while calling {0}\nReason: {1}'.format(GATEWAY_URL, e.reason)
            )

    def _zone_info(self, domain):
        result = self._call({
            'code': '0205',
            'zone': {
                'name': domain
            }
        })
        if result and result['status']['type'] == 'success':
            return result['data']['zone']
        else:
            logger.debug('Failed retrieving zone {0}'.format(domain))
            logger.debug('Response was: ' + repr(result))
            raise errors.PluginError(
                'Unable to find a SchlundTech zone for {0}'.format(domain)
            )

    def add_txt_record(self, domain, validation_name, validation):
        info = self._zone_info(domain)
        current_value = self._current_value(info, domain, validation_name, validation)
        if current_value is not None and current_value != validation:
            logger.debug('{0} already exists with value {1}'.format(validation_name, current_value))
            raise errors.PluginError(
                'TXT record {1} for {0} already exists with different value'.format(domain, validation_name)
            )
        elif current_value == validation:
            logger.debug('{0} already exists with identical value'.format(validation_name))
            pass
        else:
            result = self._call({
                'code': '0202001',
                'zone': {
                    'name': domain,
                    'system_ns': info['system_ns']
                },
                'default': {
                    'rr_add': {
                        'name': self._strip_domain(domain, validation_name),
                        'type': 'TXT',
                        'value': validation,
                        'ttl': self.ttl
                    },
                    'soa': info['soa']
                }
            })
            if result is None or result['status']['type'] != 'success':
                logger.debug('Failed adding TXT record \'{1}\' for domain \'{0}\''.format(domain, validation_name))
                logger.debug('Response was: ' + repr(result))
                msg = ('\n' + result['status']['text']) if result is not None and 'text' in result['status'] else ''
                raise errors.PluginError(
                    'Unable to add TXT record for {0}: {1}{2}'.format(domain, validation_name, msg)
                )

    def del_txt_record(self, domain, validation_name, validation):
        info = self._zone_info(domain)
        result = self._call({
            'code': '0202001',
            'zone': {
                'name': domain,
                'system_ns': info['system_ns']
            },
            'default': {
                'rr_rem': {
                    'name': _SchlundtechGatewayClient._strip_domain(domain, validation_name),
                    'type': 'TXT',
                    'value': validation,
                    'ttl': self.ttl
                }
            }
        })
        if result is None or result['status']['type'] != 'success':
            logger.debug('Failed removing TXT record \'{1}\' for domain \'{0}\''.format(domain, validation_name))
            logger.debug('Response was: ' + repr(result))
            msg = ('\n' + result['status']['text']) if result is not None and 'text' in result['status'] else ''
            raise errors.PluginError(
                'Unable to remove TXT record for {0}: {1}{2}'.format(domain, validation_name, msg)
            )

    @staticmethod
    def _current_value(info, domain, validation_name, validation):
        prefix = _SchlundtechGatewayClient._strip_domain(domain, validation_name)
        if 'rr' in info:
            if type(info['rr']) != list:
                info['rr'] = [info['rr']]  # Convert single values to list
            for rr in info['rr']:
                if rr['name'] == prefix and rr['value'] != validation:
                    return rr['value']
        return None

    @staticmethod
    def _strip_domain(domain, validation_name):
        if validation_name.endswith(domain):
            return validation_name.replace('.' + domain, '')
        else:
            return validation_name

    @staticmethod
    def _log_call_error(request, response, error):
        logger.debug('Error calling {0}'.format(GATEWAY_URL))
        logger.debug('Request was:  ' + repr(request['task']))
        if response is not None:
            logger.debug('Response was: ' + repr(response))
        if error is not None:
            logger.debug('Error was:    ' + repr(error))


class _XML:
    """
    Handles XML marshalling and unmarshalling.
    """
    def __init__(self):
        pass

    def tostring(self, tag, obj):
        return Et.tostring(self.marshal(tag, obj))

    def fromstring(self, data):
        return self.unmarshal(Et.fromstring(data))

    def _marshal_value(self, e, tag, value):
        t = type(value)
        if value is None:
            pass
        elif t in [str, int, float]:
            Et.SubElement(e, tag).text = str(value)
        elif t == list:
            for item in value:
                self._marshal_value(e, tag, item)
        elif t == dict:
            e.append(self.marshal(tag, value))
        elif hasattr(value, '__dict__'):
            e.append(self.marshal(tag, value.__dict__))
        else:
            raise NotImplementedError('Unable to serialize {0}={1}'.format(tag, value))

    def marshal(self, tag, data):
        e = Et.Element(tag)
        for name, value in data.items():
            self._marshal_value(e, name, value)
        return e

    def unmarshal(self, e):
        if e.text is not None:
            return e.text
        elif len(e) == 0:
            return None
        else:
            result = {}
            for child in e:
                name = child.tag
                value = self.unmarshal(child)
                if name in result:
                    if type(result[name]) == list:
                        result[name].append(value)
                    else:
                        result[name] = [result[name], value]
                else:
                    result[name] = value
            return result