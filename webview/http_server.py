import email.utils  # For datetime formatting
from http import HTTPStatus
import logging
import mimetypes
import os
import pathlib
import posixpath
import random
import socket
import threading
import urllib.parse
import wsgiref.simple_server
import wsgiref.util

try:
    # Python 3.7+
    import importlib.resources as importlib_resources
except ImportError:
    # Python 3.6
    import importlib_resources


from .util import abspath


__all__ = ('resolve_url', 'StaticFiles', 'StaticResources', 'Routing')

logger = logging.getLogger(__name__)


CHUNK_SIZE = 4 * 1024  # 4k


# Follow Django in treating URLs as UTF-8 encoded (which requires undoing the
# implicit ISO-8859-1 decoding applied in Python 3). Strictly speaking, URLs
# should only be ASCII anyway, but UTF-8 can be found in the wild.
def decode_path_info(path_info):
    return path_info.encode("iso-8859-1", "replace").decode("utf-8", "replace")


def send_simple_text(environ, start_response, status, body):
    """
    Send a simple message as plain text
    """
    if isinstance(status, int):
        status = "{} {}".format(int(status), status.phrase)

    if isinstance(body, str):
        body = body.encode('utf-8')

    response_headers = [
        ('Content-Type', 'text/plain'),
        ('Content-Length', str(len(body)))
    ]

    start_response(status, response_headers)
    return [body]


def do_403(environ, start_response):
    """
    Generic app to produce a 403
    """
    urlpath = environ['SCRIPT_NAME'] + environ['PATH_INFO']

    return send_simple_text(
        environ, start_response, HTTPStatus.FORBIDDEN, "Path {} is not allowed.".format(urlpath),
    )


def do_404(environ, start_response):
    """
    Generic app to produce a 404
    """
    urlpath = environ['SCRIPT_NAME'] + environ['PATH_INFO']

    return send_simple_text(
        environ, start_response, HTTPStatus.NOT_FOUND, "Path {} was not found".format(urlpath),
    )


def do_405(environ, start_response):
    """
    Generic app to produce a 405
    """
    urlpath = environ['SCRIPT_NAME'] + environ['PATH_INFO']

    return send_simple_text(
        environ, start_response, HTTPStatus.METHOD_NOT_ALLOWED,
        "Method {} is not allowed on {}".format(
            environ['REQUEST_METHOD'], urlpath,
        ),
    )


class Routing(dict):
    """
    Implements a basic URL routing system.

    Path prefixes are compared to the request path. The longest prefix wins.

    Example:
        Routing({
            '/': app,
            '/static': Static('mystatic'),
        })
    """

    def no_route_found(self, environ, start_response):
        """
        Handle if there was no matching route
        """
        return do_404(environ, start_response)

    def __call__(self, environ, start_response):
        # SCRIPT_NAME + PATH_INFO = full url
        urlpath = environ['SCRIPT_NAME'] + environ['PATH_INFO']
        if not urlpath:
            urlpath = '/'

        potentials = [
            prefix
            for prefix in self.keys()
            if posixpath.commonpath([prefix, urlpath]) == prefix
        ]
        try:
            match = max(potentials, key=len)
        except ValueError:
            # max() got an empty list, aka no matches found
            return self.no_route_found(environ, start_response)

        app = self[match]
        environ['SCRIPT_NAME'] = urlpath[:len(match)]
        environ['PATH_INFO'] = urlpath[len(match):]

        return app(environ, start_response)


class StaticContentsApp:
    """
    Base class for static serving implementatins
    """
    def method_not_allowed(self, environ, start_response):
        """
        Handle if we got something besides GET or HEAD
        """
        return do_405(environ, start_response)

    def file_not_found(self, environ, start_response):
        """
        Handle if the file cannot be found
        """
        return do_404(environ, start_response)

    def is_a_directory(self, environ, start_response):
        """
        Handle if we were given a directory
        """
        return do_404(environ, start_response)

    def no_permissions(self, environ, start_response):
        """
        Handle if we can't open the file
        """
        return do_403(environ, start_response)

    def open(path):
        """
        Return a file-like object in 'rb' mode.

        The path given is normalized.

        Add a .name attribute to the file if applicable

        Raise a FileNotFoundError, IsADirectoryError, or a PermissionError in
        case of error.
        """
        raise NotImplementedError

    def __call__(self, environ, start_response):
        path = posixpath.normpath(environ['PATH_INFO'] or '/')

        if environ['REQUEST_METHOD'] not in ('GET', 'HEAD'):
            return self.method_not_allowed(environ, start_response)

        path_options = [path]

        if path.endswith('/'):
            path_options.append(path[:-1])

        path_options.append(posixpath.join(path, 'index.html'))

        responder = None
        for option in path_options:
            try:
                file = self.open(option)
            except FileNotFoundError:
                if responder is not None:
                    responder = self.file_not_found
            except IsADirectoryError:
                if responder is not None:
                    responder = self.is_a_directory
            except PermissionError:
                if responder is not None:
                    responder = self.no_permissions
            except NotADirectoryError:
                # This can happen if we get a file with a trailing slash
                # This should only happen with the first option, and should be
                # covered by the next option
                pass
            else:
                break
        else:
            assert responder
            return responder(environ, start_response)

        if hasattr(file, 'name'):
            filename = file.name
        else:
            filename = path

        mime, _ = mimetypes.guess_type(filename, strict=False) or 'application/octect-stream'

        # NOTE: We're not doing cache control checking, because we don't
        # consistently have stat() available.

        # TODO: Type negotiation

        if 'HTTP_RANGE' in environ:
            return self._serve_partial_file(environ, start_response, file, filename, mime)
        else:
            return self._serve_whole_file(environ, start_response, file, filename, mime)

    def _default_headers(self, mime, file):
        rv = wsgiref.headers.Headers([
            ('Content-Type', mime),
            ('Accept-Ranges', 'bytes'),
            # TODO: Cache control
        ])

        if hasattr(file, 'fileno'):
            try:
                stat = os.fstat(file.fileno())
            except OSError:
                pass
            else:
                rv['Content-Length'] = str(stat.st_size)
                # rv['Last-Modified'] = email.utils.formatdate(stat.st_mtime, usegmt=True)

        return rv

    def _serve_whole_file(self, environ, start_response, file, filename, mime):
        response_headers = self._default_headers(mime, file)

        start_response('200 OK', response_headers._headers)

        if environ['REQUEST_METHOD'] == 'HEAD':
            file.close()
            return []
        else:
            wrapper = environ.get('wsgi.file_wrapper', wsgiref.util.FileWrapper)
            return wrapper(file, CHUNK_SIZE)

    def _parse_range(self, header, length):
        unit, _, ranges = header.partition('=')
        if unit != 'bytes':
            raise ValueError("Range not satisfiable: {}".format(header))

        ranges = [bit.strip().split('-') for bit in ranges.split(',')]
        start, end = ranges[0]
        start = int(start) if start else 0
        end = int(end) if end else None

        if length is not None:
            if end is not None:
                end = min(end, length)
            else:
                end = length
        return start, end

    def _compose_content_range(self, start, end, total):
        rv = 'bytes '
        if start is not None:
            rv += str(start)
        rv += '-'
        if end is not None:
            rv += str(end)
        rv += '/'
        if total is not None:
            rv += str(total)
        else:
            rv += '*'
        return rv

    def _serve_partial_file(self, environ, start_response, file, filename, mime):
        response_headers = self._default_headers(mime, file)
        length = response_headers['Content-Length']
        if length:
            length = int(length)
        else:
            length = None
        start, end = self._parse_range(environ['HTTP_RANGE'], length)

        # TODO: Handle unsatisfiable ranges

        response_headers['Content-Range'] = self._compose_content_range(start, end, length)
        if end is None:
            del response_headers['Content-Length']
        else:
            response_headers['Content-Length'] = str(end - start)

        start_response('206 Partial Content', response_headers._headers)

        if environ['REQUEST_METHOD'] == 'HEAD':
            file.close()
            return []
        else:
            return self._partial_file_wrapper(file, start, end)

    def _partial_file_wrapper(self, file, start, end):
        if start:
            file.seek(start)

        total = 0
        if end is not None:
            expected = end - (start or 0)
        else:
            expected = None

        while (expected is None) or (total < expected):
            data = file.read(min(CHUNK_SIZE, expected - total))
            total += len(data)
            yield data


class StaticFiles(StaticContentsApp):
    """
    Serves static files from a directory on the file system.
    """
    def __init__(self, root):
        self.root = abspath(root)

    def open(self, file):
        path = os.path.join(self.root, file.lstrip('/'))
        return open(path, 'rb')


class StaticResources(StaticContentsApp):
    """
    Serves static files from resources in python packages
    """
    def __init__(self, root):
        self.root = root

    def open(self, file):
        slashed, basename = posixpath.split(file)
        slashed = slashed.rstrip('/')
        if slashed:
            packagename = "{}.{}".format(self.root, slashed.replace('/', '.'))
        else:
            packagename = self.root
        return importlib_resources.open_binary(packagename, basename)


def _get_random_port():
    while True:
        port = random.randint(1023, 65535)

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(('localhost', port))
            except OSError:
                logger.warning('Port %s is in use' % port)
                continue
            else:
                return port


def get_wsgi_server(app):
    if hasattr(app, '__webview_url'):
        # It's already been spun up and is running
        return app.__webview_url

    port = _get_random_port()
    server = wsgiref.simple_server.make_server('localhost', port, app)

    t = threading.Thread(target=server.serve_forever)
    t.daemon = True
    t.start()

    app.__webview_url = 'http://localhost:{0}/'.format(port)
    logger.debug('HTTP server for {!r} started on {}'.format(app, app.__webview_url))

    return app.__webview_url


_path_apps = {}


def resolve_url(url, should_serve):
    """
    Given a URL-ish thing and a bool, return a real URL.

    * url: A URL, a path-like, a string path, or a wsgi app
    * should_serve: Should we start a server

    Note that if given a wsgi app, a server will always be started.
    """
    if isinstance(url, str):
        bits = urllib.parse.urlparse(url)
    else:
        # To create an empty version of the struct
        bits = urllib.parse.urlparse("")

    if bits.scheme and bits.scheme != 'file':
        # an http, https, etc URL
        return url
    elif hasattr(url, '__fspath__') or isinstance(url, str):
        # A local path

        # 1. Resolve the several options into an actual path
        if hasattr(url, '__fspath__'):
            path = os.fspath(url)
        elif bits.scheme == 'file':
            path = bits.path
        else:
            path = url

        # If it's a relative path, resolve it relative to the app root
        path = abspath(path)

        # If we have not been asked to serve local paths, bail
        if not should_serve:
            # using pathlib for this because it turns out file URLs are full of dragons
            return pathlib.Path(path).as_uri()

        # Get/Build a WSGI app to serve the path and spin it up
        if path not in _path_apps:
            _path_apps[path] = StaticFiles(path)
        return get_wsgi_server(_path_apps[path])
    elif callable(url):
        # A wsgi application
        return get_wsgi_server(url)
    else:
        raise TypeError("Cannot resolve {!r} into a URL".format(url))
