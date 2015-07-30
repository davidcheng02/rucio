"""
  Copyright European Organization for Nuclear Research (CERN)

  Licensed under the Apache License, Version 2.0 (the "License");
  You may not use this file except in compliance with the License.
  You may obtain a copy of the License at http://www.apache.org/licenses/LICENSE-2.0

  Authors:
  - Vincent Garonne, <vincent.garonne@cern.ch>, 2012-2014
  - Thomas Beermann, <thomas.beermann@cern.ch>, 2012-2013
  - Cedric Serfon, <cedric.serfon@cern.ch>, 2014-2015
  - Ralph Vigne, <ralph.vigne@cern.ch>, 2015

Client class for callers of the Rucio system
"""

import random
import sys

from getpass import getuser
from logging import getLogger, StreamHandler, ERROR
from os import environ, fdopen, path, makedirs
from shutil import move
from tempfile import mkstemp
from urlparse import urlparse

from ConfigParser import NoOptionError, NoSectionError
from dogpile.cache import make_region
from requests import session
from requests.status_codes import codes, _codes
from requests.exceptions import SSLError
from requests_kerberos import HTTPKerberosAuth
# See https://github.com/kennethreitz/requests/issues/2214
from requests.packages.urllib3 import disable_warnings
disable_warnings()

from rucio.common import exception
from rucio.common.config import config_get
from rucio.common.exception import CannotAuthenticate, ClientProtocolNotSupported, NoAuthInformation, MissingClientParameter
from rucio.common.utils import build_url, my_key_generator, parse_response
from rucio import version

LOG = getLogger(__name__)
SH = StreamHandler()
SH.setLevel(ERROR)
LOG.addHandler(SH)


REGION = make_region(function_key_generator=my_key_generator).configure(
    'dogpile.cache.memory',
    expiration_time=60,
)


@REGION.cache_on_arguments(namespace='host_to_choose')
def choice(hosts):
    """
    Select randomly a host

    :param hosts: Lost of hosts
    :return: A randomly selected host.
    """
    return random.choice(hosts)


class BaseClient(object):

    """Main client class for accessing Rucio resources. Handles the authentication."""

    AUTH_RETRIES = 2
    REQUEST_RETRIES = 3
    TOKEN_PATH_PREFIX = '/tmp/' + getuser() + '/.rucio_'
    TOKEN_PREFIX = 'auth_token_'

    def __init__(self, rucio_host=None, auth_host=None, account=None, ca_cert=None, auth_type=None, creds=None, timeout=None, user_agent='rucio-clients'):
        """
        Constructor of the BaseClient.
        :param rucio_host: the address of the rucio server, if None it is read from the config file.
        :param rucio_port: the port of the rucio server, if None it is read from the config file.
        :param auth_host: the address of the rucio authentication server, if None it is read from the config file.
        :param auth_port: the port of the rucio authentication server, if None it is read from the config file.
        :param account: the account to authenticate to rucio.
        :param use_ssl: enable or disable ssl for commucation. Default is enabled.
        :param ca_cert: the path to the rucio server certificate.
        :param auth_type: the type of authentication (e.g.: 'userpass', 'kerberos' ...)
        :param creds: a dictionary with credentials needed for authentication.
        :param user_agent: indicates the client
        """

        self.host = rucio_host
        self.list_hosts = []
        self.auth_host = auth_host
        self.session = session()
        self.user_agent = "%s/%s" % (user_agent, version.version_string())  # e.g. "rucio-clients/0.2.13"
        sys.argv[0] = sys.argv[0].split('/')[-1]
        self.script_id = '::'.join(sys.argv[0:2])
        if self.script_id == '':  # Python interpreter used
            self.script_id = 'python'
        try:
            if self.host is None:
                self.host = config_get('client', 'rucio_host')
            if self.auth_host is None:
                self.auth_host = config_get('client', 'auth_host')
        except (NoOptionError, NoSectionError) as error:
            raise MissingClientParameter('Section client and Option \'%s\' cannot be found in config file' % error.args[0])

        self.account = account
        self.ca_cert = ca_cert
        self.auth_type = auth_type
        self.creds = creds
        self.auth_token = None
        self.headers = {}
        self.timeout = timeout
        self.request_retries = self.REQUEST_RETRIES

        if auth_type is None:
            LOG.debug('no auth_type passed. Trying to get it from the environment variable RUCIO_AUTH_TYPE and config file.')
            if 'RUCIO_AUTH_TYPE' in environ:
                if environ['RUCIO_AUTH_TYPE'] not in ('userpass', 'x509', 'x509_proxy', 'gss'):
                    raise MissingClientParameter('Possible RUCIO_AUTH_TYPE values: userpass, x509, x509_proxy, gss vs. ' + environ['RUCIO_AUTH_TYPE'])
                self.auth_type = environ['RUCIO_AUTH_TYPE']
            else:
                try:
                    self.auth_type = config_get('client', 'auth_type')
                except (NoOptionError, NoSectionError) as error:
                    raise MissingClientParameter('Option \'%s\' cannot be found in config file' % error.args[0])

        if creds is None:
            LOG.debug('no creds passed. Trying to get it from the config file.')
            self.creds = {}
            try:
                if self.auth_type == 'userpass':
                    self.creds['username'] = config_get('client', 'username')
                    self.creds['password'] = config_get('client', 'password')
                elif self.auth_type == 'x509':
                    self.creds['client_cert'] = path.abspath(path.expanduser(path.expandvars(config_get('client', 'client_cert'))))
                    self.creds['client_key'] = path.abspath(path.expanduser(path.expandvars(config_get('client', 'client_key'))))
                elif self.auth_type == 'x509_proxy':
                    self.creds['client_proxy'] = path.abspath(path.expanduser(path.expandvars(config_get('client', 'client_x509_proxy'))))
            except (NoOptionError, NoSectionError) as error:
                if error.args[0] != 'client_key':
                    raise MissingClientParameter('Option \'%s\' cannot be found in config file' % error.args[0])

        rucio_scheme = urlparse(self.host).scheme
        auth_scheme = urlparse(self.auth_host).scheme

        if rucio_scheme != 'http' and rucio_scheme != 'https':
            raise ClientProtocolNotSupported('\'%s\' not supported' % rucio_scheme)

        if auth_scheme != 'http' and auth_scheme != 'https':
            raise ClientProtocolNotSupported('\'%s\' not supported' % auth_scheme)

        if (rucio_scheme == 'https' or auth_scheme == 'https') and ca_cert is None:
            LOG.debug('no ca_cert passed. Trying to get it from the config file.')
            try:
                self.ca_cert = path.expandvars(config_get('client', 'ca_cert'))
            except (NoOptionError, NoSectionError) as error:
                raise MissingClientParameter('Option \'%s\' cannot be found in config file' % error.args[0])

        self.list_hosts = [self.host]

        if account is None:
            LOG.debug('no account passed. Trying to get it from the config file.')
            try:
                self.account = config_get('client', 'account')
            except (NoOptionError, NoSectionError):
                try:
                    self.account = environ['RUCIO_ACCOUNT']
                except KeyError:
                    raise MissingClientParameter('Option \'account\' cannot be found in config file and RUCIO_ACCOUNT is not set.')

        token_path = self.TOKEN_PATH_PREFIX + self.account
        self.token_file = token_path + '/' + self.TOKEN_PREFIX + self.account

        self.__authenticate()

        try:
            self.request_retries = int(config_get('client', 'request_retries'))
        except NoOptionError:
            LOG.debug('request_retries not specified in config file. Taking default.')
        except ValueError:
            LOG.debug('request_retries must be an integer. Taking default.')

    def _get_exception(self, headers, status_code=None, data=None):
        """
        Helper method to parse an error string send by the server and transform it into the corresponding rucio exception.

        :param headers: The http response header containing the Rucio exception details.
        :param status_code: The http status code.
        :param data: The data with the ExceptionMessage.

        :return: A rucio exception class and an error string.
        """
        data = parse_response(data)
        if 'ExceptionClass' not in data:
            if 'ExceptionMessage' not in data:
                human_http_code = _codes.get(status_code, None)  # NOQA, pylint: disable-msg=W0612
                return getattr(exception, 'RucioException'), 'no error information passed (http status code: %(status_code)s %(human_http_code)s)' % locals()
            return getattr(exception, 'RucioException'), data['ExceptionMessage']

        exc_cls = None
        try:
            exc_cls = getattr(exception, data['ExceptionClass'])
        except AttributeError:
            return getattr(exception, 'RucioException'), data['ExceptionMessage']

        return exc_cls, data['ExceptionMessage']

    def _load_json_data(self, response):
        """
        Helper method to correctly load json data based on the content type of the http response.

        :param response: the response received from the server.
        """
        if 'content-type' in response.headers and response.headers['content-type'] == 'application/x-json-stream':
            for line in response.iter_lines():
                if line:
                    yield parse_response(line)
        elif 'content-type' in response.headers and response.headers['content-type'] == 'application/json':
            yield parse_response(response.text)
        else:  # Exception ?
            yield response.text

    def _send_request(self, url, headers=None, type='GET', data=None, params=None):
        """
        Helper method to send requests to the rucio server. Gets a new token and retries if an unauthorized error is returned.

        :param url: the http url to use.
        :param headers: additional http headers to send.
        :param type: the http request type to use.
        :param data: post data.
        :param params: (optional) Dictionary or bytes to be sent in the url query string.
        :return: the HTTP return body.
        """
        result, retry = None, 0
        hds = {'X-Rucio-Auth-Token': self.auth_token, 'X-Rucio-Account': self.account,
               'Connection': 'Keep-Alive', 'User-Agent': self.user_agent,
               'X-Rucio-Script': self.script_id}

        if headers is not None:
            hds.update(headers)

        while retry <= self.request_retries:
            try:
                if type == 'GET':
                    result = self.session.get(url, headers=hds, verify=self.ca_cert, timeout=self.timeout, params=params, stream=True)
                elif type == 'PUT':
                    result = self.session.put(url, headers=hds, data=data, verify=self.ca_cert, timeout=self.timeout)
                elif type == 'POST':
                    result = self.session.post(url, headers=hds, data=data, verify=self.ca_cert, timeout=self.timeout, stream=True)
                elif type == 'DEL':
                    result = self.session.delete(url, headers=hds, data=data, verify=self.ca_cert, timeout=self.timeout)
                else:
                    return
#             except ConnectionError, e:
#                 LOG.warning('ConnectionError: ' + str(e))
#                 retry += 1
#                 if retry > self.request_retries:
#                     raise
#                 continue
            except SSLError as error:
                LOG.warning('SSLError: ' + str(error))
                retry += 1
                self.ca_cert = False
                if retry > self.request_retries:
                    raise
                continue

            if result is not None and result.status_code == codes.unauthorized:  # pylint: disable-msg=E1101
                self.__get_token()
                hds['X-Rucio-Auth-Token'] = self.auth_token
                retry += 1
            else:
                break
        return result

    def __get_token_userpass(self):
        """
        Sends a request to get an auth token from the server and stores it as a class attribute. Uses username/password.

        :returns: True if the token was successfully received. False otherwise.
        """

        headers = {'X-Rucio-Account': self.account, 'X-Rucio-Username': self.creds['username'], 'X-Rucio-Password': self.creds['password']}
        url = build_url(self.auth_host, path='auth/userpass')

        retry = 0
        while retry <= self.AUTH_RETRIES:
            try:
                result = self.session.get(url, headers=headers, verify=self.ca_cert)
            except SSLError as error:
                LOG.warning('SSLError: ' + str(error))
                self.ca_cert = False
                retry += 1
                if retry > self.request_retries:
                    raise
                continue
            break

        if retry == 2:
            LOG.error('cannot get auth_token')
            return False

        if result.status_code != codes.ok:  # pylint: disable-msg=E1101
            exc_cls, exc_msg = self._get_exception(headers=result.headers,
                                                   status_code=result.status_code,
                                                   data=result.content)
            raise exc_cls(exc_msg)

        self.auth_token = result.headers['x-rucio-auth-token']
        LOG.debug('got new token \'%s\'' % self.auth_token)
        return True

    def __get_token_x509(self):
        """
        Sends a request to get an auth token from the server and stores it as a class attribute. Uses x509 authentication.

        :returns: True if the token was successfully received. False otherwise.
        """

        headers = {'X-Rucio-Account': self.account}

        client_cert = None
        client_key = None
        if self.auth_type == 'x509':
            url = build_url(self.auth_host, path='auth/x509')
            client_cert = self.creds['client_cert']
            if 'client_key' in self.creds:
                client_key = self.creds['client_key']
        elif self.auth_type == 'x509_proxy':
            url = build_url(self.auth_host, path='auth/x509_proxy')
            client_cert = self.creds['client_proxy']

        if not path.exists(client_cert):
            LOG.error('given client cert (%s) doesn\'t exist' % client_cert)
            return False
        if client_key is not None and not path.exists(client_key):
            LOG.error('given client key (%s) doesn\'t exist' % client_key)

        retry = 0
        result = None

        if client_key is None:
            cert = client_cert
        else:
            cert = (client_cert, client_key)

        while retry <= self.AUTH_RETRIES:
            try:
                result = self.session.get(url, headers=headers, cert=cert,
                                          verify=self.ca_cert)
            except SSLError as error:
                if 'alert certificate expired' in str(error):
                    raise CannotAuthenticate(str(error))
                LOG.warning('SSLError: ' + str(error))
                self.ca_cert = False
                retry += 1
                if retry > self.request_retries:
                    raise
                continue
            break

        if retry == 2:
            LOG.error('cannot get auth_token')
            return False

        if result and result.status_code != codes.ok:   # pylint: disable-msg=E1101
            exc_cls, exc_msg = self._get_exception(headers=result.headers,
                                                   status_code=result.status_code,
                                                   data=result.content)
            raise exc_cls(exc_msg)

        self.auth_token = result.headers['x-rucio-auth-token']
        LOG.debug('got new token \'%s\'' % self.auth_token)
        return True

    def __get_token_gss(self):
        """
        Sends a request to get an auth token from the server and stores it as a class attribute. Uses Kerberos authentication.

        :returns: True if the token was successfully received. False otherwise.
        """

        headers = {'X-Rucio-Account': self.account}
        url = build_url(self.auth_host, path='auth/gss')

        retry = 0
        while retry <= self.AUTH_RETRIES:
            try:
                result = self.session.get(url, headers=headers,
                                          verify=self.ca_cert, auth=HTTPKerberosAuth())
            except SSLError as error:
                LOG.warning('SSLError: ' + str(error))
                self.ca_cert = False
                retry += 1
                if retry > self.request_retries:
                    raise
                continue
            break

        if retry == 2:
            LOG.error('cannot get auth_token')
            return False

        if result.status_code != codes.ok:   # pylint: disable-msg=E1101
            exc_cls, exc_msg = self._get_exception(headers=result.headers,
                                                   status_code=result.status_code,
                                                   data=result.content)
            raise exc_cls(exc_msg)

        self.auth_token = result.headers['x-rucio-auth-token']
        LOG.debug('got new token \'%s\'' % self.auth_token)
        return True

    def __get_token(self):
        """
        Calls the corresponding method to receive an auth token depending on the auth type. To be used if a 401 - Unauthorized error is received.
        """

        retry = 0
        LOG.debug('get a new token')
        while retry <= self.AUTH_RETRIES:
            if self.auth_type == 'userpass':
                if not self.__get_token_userpass():
                    raise CannotAuthenticate('userpass authentication failed')
            elif self.auth_type == 'x509' or self.auth_type == 'x509_proxy':
                if not self.__get_token_x509():
                    raise CannotAuthenticate('x509 authentication failed')
            elif self.auth_type == 'gss':
                if not self.__get_token_gss():
                    raise CannotAuthenticate('kerberos authentication failed')
            else:
                raise CannotAuthenticate('auth type \'%s\' not supported' % self.auth_type)

            if self.auth_token is not None:
                self.__write_token()
                self.headers['X-Rucio-Auth-Token'] = self.auth_token
                break

            retry += 1

        if self.auth_token is None:
            raise CannotAuthenticate('cannot get an auth token from server')

    def __read_token(self):
        """
        Checks if a local token file exists and reads the token from it.

        :return: True if a token could be read. False if no file exists.
        """
        if not path.exists(self.token_file):
            return False

        try:
            token_file_handler = open(self.token_file, 'r')
            self.auth_token = token_file_handler.readline()
            self.headers['X-Rucio-Auth-Token'] = self.auth_token
        except IOError as (errno, strerror):  # NOQA
            print("I/O error({0}): {1}".format(errno, strerror))
        except Exception:
            raise

        LOG.debug('read token \'%s\' from file' % self.auth_token)
        return True

    def __write_token(self):
        """
        Write the current auth_token to the local token file.
        """

        token_path = self.TOKEN_PATH_PREFIX + self.account
        self.token_file = token_path + '/' + self.TOKEN_PREFIX + self.account

        # check if rucio temp directory is there. If not create it with permissions only for the current user
        if not path.isdir(token_path):
            try:
                LOG.debug('rucio token folder \'%s\' not found. Create it.' % token_path)
                makedirs(token_path, 0700)
            except Exception:
                raise

        # if the file exists check if the stored token is valid. If not request a new one and overwrite the file. Otherwise use the one from the file
        try:
            file_d, file_n = mkstemp(dir=token_path)
            with fdopen(file_d, "w") as f_token:
                f_token.write(self.auth_token)
            move(file_n, self.token_file)
        except IOError as (errno, strerror):  # NOQA
            print("I/O error({0}): {1}".format(errno, strerror))
        except Exception:
            raise

    def __authenticate(self):
        """
        Main method for authentication. It first tries to read a locally saved token. If not available it requests a new one.
        """

        if self.auth_type == 'userpass':
            if self.creds['username'] is None or self.creds['password'] is None:
                raise NoAuthInformation('No username or password passed')
        elif self.auth_type == 'x509':
            if self.creds['client_cert'] is None:
                raise NoAuthInformation('The path to the client certificate is required')
        elif self.auth_type == 'x509_proxy':
            if self.creds['client_proxy'] is None:
                raise NoAuthInformation('The client proxy has to be defined')
        elif self.auth_type == 'gss':
            pass
        else:
            raise CannotAuthenticate('auth type \'%s\' not supported' % self.auth_type)

        if not self.__read_token():
            self.__get_token()
