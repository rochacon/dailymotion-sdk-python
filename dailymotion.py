""" Dailymotion SDK """
import requests
import time
import os
import sys
import shelve
import pprint
import re
import pycurl
import json
import StringIO

__author__ = 'Samir AMZANI <samir.amzani@gmail.com>'
__version__ = '0.1.0'
__python_version__ = '.'.join([str(i) for i in sys.version_info[:3]])

try:
    from urllib.parse import urlencode
except ImportError:  # Python 2
    from urllib import urlencode

try:
    from urllib.parse import parse_qsl
except ImportError:  # Python 2
    try:
        from urlparse import parse_qsl
    except ImportError:  # Python < 2.6
        from cgi import parse_qsl


class SessionStore(object):

    def __init__(self, user = 'default'):
        self._user = user
        self.set_file_store_backend()

    def set_file_store_backend(self):
        backend_file = '%s/.dailymotion_sdk_session_%s' % (os.path.expanduser('~'), self._user)
        self._backend = shelve.open('%s/.dailymotion_sdk_session_%s' % (os.path.expanduser('~'), self._user))
        os.chmod('%s.db' % backend_file, 0600)

    def get(self, key, default=None):
        return self._backend.get(key, default)

    def update(self, dict):
        self._backend.update(dict)

    def set(self, key, value):
        self._backend[key] = value

    def delete(self, key):
        if key in self._backend:
            del self._backend[key]

    def clear(self):
        self._backend.clear()

    def close(self):
        self._backend.close()



class DailymotionClientError(Exception):
    def __init__(self, message, error_type=None):
        self.type = error_type

        self.message = message
        if error_type is not None:
            self.message = '%s: %s' % (error_type, message)

        super(DailymotionClientError, self).__init__(self.message)


class DailymotionApiError(DailymotionClientError): pass
class DailymotionAuthError(DailymotionClientError): pass
class DailymotionTokenExpired(DailymotionClientError): pass
class DailymotionUploadTransportError(DailymotionClientError): pass
class DailymotionUploadInvalidResponse(DailymotionClientError): pass
class DailymotionUploadError(DailymotionClientError): pass


class Dailymotion(object):

    DEFAULT_DEBUG           = False
    DEFAULT_TIMEOUT         = 5
    DEFAULT_API_BASE_URL    = 'https://api.dailymotion.com'
    DEFAULT_AUTHORIZE_URL   = 'https://www.dailymotion.com/oauth/authorize'
    DEFAULT_TOKEN_URL       = 'https://api.dailymotion.com/oauth/token'
    DEFAULT_SESSION_STORE   = True

    def __init__(self, api_base_url=None, debug=None, timeout=None, oauth_authorize_endpoint_url=None, oauth_token_endpoint_url=None, session_store_enabled=None):

        self.api_base_url                   = api_base_url or self.DEFAULT_API_BASE_URL
        self.debug                          = debug or self.DEFAULT_DEBUG
        self.timeout                        = timeout or self.DEFAULT_TIMEOUT
        self.oauth_authorize_endpoint_url   = oauth_authorize_endpoint_url or self.DEFAULT_AUTHORIZE_URL
        self.oauth_token_endpoint_url       = oauth_token_endpoint_url or self.DEFAULT_TOKEN_URL
        self._grant_type                    = None
        self._grant_info                    = {}
        self._headers                       = {'Accept' : 'application/json',
                                                'User-Agent' : 'Dailymotion-Python/%s (Python %s)' % (__version__, __python_version__)}
        self._session_store_enabled         = session_store_enabled or self.DEFAULT_SESSION_STORE
        self._session_store                 = None
        

    def set_grant_type(self, grant_type = 'client_credentials', api_key=None, api_secret=None, scope=None, info=None):
        
        """
        Grant types:
         - token:
            An authorization is requested to the end-user by redirecting it to an authorization page hosted
            on Dailymotion. Once authorized, a refresh token is requested by the API client to the token
            server and stored in the end-user's cookie (or other storage technique implemented by subclasses).
            The refresh token is then used to request time limited access token to the token server.

        - none / client_credentials:
            This grant type is a 2 legs authentication: it doesn't allow to act on behalf of another user.
            With this grant type, all API requests will be performed with the user identity of the API key owner.

        - password:
            This grant type allows to authenticate end-user by directly providing its credentials.
            This profile is highly discouraged for web-server workflows. If used, the username and password
            MUST NOT be stored by the client.
        """

        if api_key and api_secret:
            self._grant_info['key'] = api_key
            self._grant_info['secret'] = api_secret
        else:
            raise DailymotionClientError('Missing API key/secret')

        if isinstance(info, dict):
            self._grant_info.update(info)
        else:
            info = {}

        if self._session_store_enabled:
            self._session_store = SessionStore(info.get('username', 'default'))

        if grant_type in ('authorization', 'token'):
            grant_type = 'authorization'
            if 'redirect_uri' not in info:
                raise DailymotionClientError('Missing redirect_uri in grant info for token grant type.')
        elif grant_type in ('client_credentials', 'none'):
            grant_type = 'client_credentials'
        elif grant_type == 'password':
            if 'username' not in info or 'password' not in info:
                raise DailymotionClientError('Missing username or password in grant info for password grant type.')
        else:
            raise DailymotionClientError('Invalid grant type %s.' % grant_type)
        
        self._grant_type = grant_type

        if scope:
            if not isinstance(scope, (list, tuple)):
                raise DailymotionClientError('Invalid scope type: must be a list of valid scopes')
            self._grant_info['scope'] = scope


    def get_authorization_url(self, redirect_uri=None, scope=None, display='page'):
        if self._grant_type != 'authorization':
            raise DailymotionClientError('This method can only be used with TOKEN grant type.')

        qs = {
            'response_type': 'code',
            'client_id': self._grant_info['key'],
            'redirect_uri': redirect_uri,
            'display': display,
            }
        if scope and type(scope) in (list, tuple):
            qs['scope'] =  ' '.join(scope)

        return '%s?%s' % (self.oauth_authorize_endpoint_url, urlencode(qs))

    def oauth_token_request(self, params):
        try:
            result = self.request(self.oauth_token_endpoint_url, 'POST', params)
        except DailymotionApiError, e:
            raise DailymotionAuthError(str(e))

        if 'error' in result:
            raise DailymotionAuthError(result.get('error_description',''))

        if 'access_token' not in result:
            raise DailymotionAuthError("Invalid token server response : ", str(result))

        result = {
            'access_token': result['access_token'],
            'expires': int(time.time() + int(result['expires_in']) * 0.85), # refresh at 85% of expiration time for safety
            'refresh_token': result['refresh_token'] if 'refresh_token' in result else None,
            'scope': result['scope'] if 'scope' in result else [],
            }

        if self._session_store_enabled and self._session_store != None:
            self._session_store.update(result)
        return result

    def get_access_token(self, force_refresh=False, request_args=None):
        params = {}

        if self._grant_type == None:
            return None

        if self._session_store_enabled and self._session_store != None:
            access_token = self._session_store.get('access_token')
            if access_token and not force_refresh and time.time() < self._session_store.get('expires', 0):
                return access_token

        if self._session_store_enabled and self._session_store != None:
            refresh_token = self._session_store.get('refresh_token')
            if refresh_token:
                params = {
                    'grant_type': 'refresh_token',
                    'client_id': self._grant_info['key'],
                    'client_secret': self._grant_info['secret'],
                    'scope': ' '.join(self._grant_info['scope']) if 'scope' in self._grant_info and self._grant_info['scope'] else '',
                    'refresh_token': refresh_token,
                    }
                response = self.oauth_token_request(params)
                return response.get('access_token')

        if self._grant_type == 'authorization':
            if request_args and 'code' in request_args:
                params = {
                    'grant_type': 'authorization_code',
                    'client_id': self._grant_info['key'],
                    'client_secret': self._grant_info['secret'],
                    'redirect_uri': self._grant_info['redirect_uri'],
                    'scope': ' '.join(self._grant_info['scope']) if 'scope' in self._grant_info and self._grant_info['scope'] else '',
                    'code': request_args['code'],
                    }

            elif request_args and 'error' in request_args:
                error_msg = request_args.get('error_description')
                if request_args['error'] == 'error_description':
                    raise DailymotionAuthError(error_msg)
                else:
                    raise DailymotionAuthError(error_msg)
            else:
                raise AuthRequired()
        elif self._grant_type in ('password', 'client_credentials'):
            params = {
                'grant_type': self._grant_type,
                'client_id': self._grant_info['key'],
                'client_secret': self._grant_info['secret'],
                'scope': ' '.join(self._grant_info['scope']) if 'scope' in self._grant_info and self._grant_info['scope'] else '',
                'username': self._grant_info['username'],
                'password': self._grant_info['password']
                }

        response = self.oauth_token_request(params)
        return response.get('access_token')

    def logout(self):
        self.call('/logout')
        self._session_store.clear()



    def get(self, endpoint, params=None):
        return self.call(endpoint, params=params)


    def post(self, endpoint, params=None, files=None):
        return self.call(endpoint, method='POST', params=params)


    def delete(self, endpoint, params=None):
        return self.call(endpoint, method='DELETE', params=params)


    def call(self, endpoint, method='GET', params=None, files=None):
        try:
            access_token = self.get_access_token()
            if access_token:
                self._headers['Authorization'] = 'Bearer %s' % access_token
            return self.request(endpoint, method, params, files)
        except DailymotionTokenExpired:
            access_token = 'Bearer %s' % self.get_access_token(True)
            if access_token:
                self._headers['Authorization'] = 'Bearer %s' % access_token

        return self.request(endpoint, method, params, files)

    def upload(self, file_path, progress=None):
        return self.upload_with_pycurl(file_path, progress)

    def upload_with_requests(self, file_path, progress=None):
        """
        Not implemented
        """
        pass

    def upload_with_pycurl(self, file_path, progress=None):
        if not os.path.exists(file_path):
            raise IOError("[Errno 2] No such file or directory: '%s'" % file_path)
        result = self.call('/file/upload')

        if isinstance(file_path, unicode):
            file_path = file_path.encode('utf8')
        file_path = os.path.abspath(os.path.expanduser(file_path))

        c = pycurl.Curl()
        c.setopt(pycurl.URL, str(result['upload_url']))
        c.setopt(pycurl.USERAGENT, 'Dailymotion-Python/%s (Python %s)' % (__version__, __python_version__))
        c.setopt(pycurl.HTTPHEADER, ['Expect:'])
        c.setopt(pycurl.FOLLOWLOCATION, True)
        c.setopt(pycurl.HTTPPOST, [('file', (pycurl.FORM_FILE, file_path))])

        if progress:
            c.setopt(pycurl.NOPROGRESS, 0)
            c.setopt(pycurl.PROGRESSFUNCTION, lambda x, y, total, current: progress(current, total))

        response = StringIO.StringIO()
        c.setopt(pycurl.WRITEFUNCTION, response.write)

        try:
            c.perform()
        except pycurl.error, e:
            raise DailymotionUploadTransportError('%s: %s' % (result['upload_url'], e))
        c.close()

        try:
            res = response.getvalue()
        except UnicodeError, e:
            raise DailymotionUploadInvalidResponse('Invalid API server response: %s' % str(e))
        try:
            response = json.loads(res)
        except ValueError, e:
            raise DailymotionUploadInvalidResponse('Invalid API server response "%s": %s' % (res, str(e)))
        if 'error' in response:
            raise DailymotionUploadError(response['error'])

        return response['url']

    def request(self, endpoint, method='GET', params=None, files=None):
        params = params or {}

        if endpoint.find('http') == 0:
            url = endpoint
        else:
            if endpoint.find('/') != 0:
                raise DailymotionClientError('Endpoint must start with / (eg:/me/video)')
            url = '%s%s' % (self.api_base_url, endpoint)
        
        method = method.lower()

        if not method in ('get', 'post', 'delete'):
            raise DailymotionClientError('Method must be of GET, POST or DELETE')

        func = getattr(requests, method)
        try:
            if method == 'get':
                response = func(url, params=params, headers=self._headers, timeout=self.timeout)
            else:
                response = func(url,
                                data=params,
                                files=files,
                                headers=self._headers,
                                timeout=self.timeout)

        except requests.exceptions.ConnectionError:
            raise DailymotionClientError('Network problem (DNS failure, refused connection...).')
        except requests.exceptions.HTTPError:
            raise DailymotionClientError('Invalid HTTP response')
        except requests.exceptions.Timeout:
            raise DailymotionApiError('The request times out, current timeout is = %s' % self.timeout)
        except requests.exceptions.TooManyRedirects:
            raise DailymotionApiError('The request exceeds the configured number of maximum redirections')
        except requests.exceptions.RequestException:
            raise DailymotionClientError('An unknown error occurred.')

        try:
            content = response.json()
        except ValueError:
            raise DailymotionApiError('Unable to parse response, invalid JSON.')
        

        if response.status_code != 200:
            if content.get('error') is not None:
                if response.status_code in (400, 401, 403):
                    authenticate_header = response.headers.get('www-authenticate')
                    if authenticate_header:
                        m = re.match('.*error="(.*?)"(?:, error_description="(.*?)")?', authenticate_header)
                        if m:
                            error = m.group(1)
                            msg = m.group(2)
                            if error == 'expired_token':
                                raise DailymotionTokenExpired(msg, error_type=error)
                        raise DailymotionAuthError(msg, error_type='auth_error')

                error = content['error']
                error_type = error.get('type', '')
                error_message = error.get('message', '')

                raise DailymotionApiError(error_message, error_type=error_type)

        return content
