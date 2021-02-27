#!/usr/bin/env python3
import re
import os
import json
import sys
import mimetypes
from functools import partial
from typing import Set, Callable, Type, Any, Dict, List, Optional, Tuple
from dataclasses import dataclass
from pprint import pprint, pformat
from collections import defaultdict
from itertools import chain
import socket # For gethostbyaddr()
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler, HTTPStatus, test as _http_server_test
import shutil


def _full_stack():
	import traceback, sys
	exc = sys.exc_info()[0]
	stack = traceback.extract_stack()[:-1]  # last one would be full_stack()
	if exc is not None:  # i.e. an exception is present
		del stack[-1]      # remove call of full_stack, the printed exception
		                   # will contain the caught exception caller instead
	trc = 'Traceback (most recent call last):\n'
	stackstr = trc + ''.join(traceback.format_list(stack))
	if exc is not None:
		stackstr += '  ' + traceback.format_exc().lstrip(trc)
	return stackstr


@dataclass
class Request:
	method: str
	path: str

@dataclass
class Response:
	status_code: int
	headers: Dict[str,Any]
	body: str

	def __init__(self, body:str, status_code:int = 200, headers: Dict[str,Any] = None):
		self.status_code = status_code
		self.headers = headers or dict()
		self.body = body

	def _write_headers(self, handler):
		status_phrases = {status: status.phrase for status in HTTPStatus.__members__.values()}
		handler.send_response(self.status_code, status_phrases[self.status_code])
		for key, value in self.headers.items():
			handler.send_header(key, value)
		handler.end_headers()

	def write(self, handler):
		body = str(self.body).encode('utf-8', 'replace')
		self.headers['Content-Length'] = len(body)
		self._write_headers(handler)
		handler.wfile.write(body)


class FileResponse(Response):
	def __init__(self, fh, status_code:int = 200, headers: Dict[str,Any] = None):
		super().__init__('', status_code, headers)
		self.fh = fh

	def write(self, handler):
		self._write_headers(handler)
		shutil.copyfileobj(self.fh, handler.wfile)

	def __del__(self):
		self.fh.close()


@dataclass
class URLType:
	pattern: str
	format: str
	cast: Type


@dataclass
class Route:
	name: str
	methods: Set[str] 
	callback: Callable
	path_expression: re.Pattern
	path_format: str
	path_placeholders: Dict[str,URLType]


class Application:
	def __init__(self):
		self.url_types = {
			'any': URLType(r'.*', '!s', str),
			'str': URLType(r'[^/]+', '!s', str),
			'int': URLType(r'\d+', ':d', int),
		}

		self.routes = []

	def route(self, route: str, methods: Set[str] = {'GET'}, name: Optional[str] = None) -> Callable[[Callable], Callable]:
		routes = self.routes
		def register(fn):
			routes.append(self.compile_route(
				path_pattern=route,
				name=name or fn.__name__,
				callback=fn,
				methods=methods))
			return fn
		return register

	def compile_route(self, path_pattern: str, **kwargs) -> re.Pattern:
		path_expression = ''
		path_format = ''
		path_placeholders = {}
		last_pos = 0
		
		for match in re.finditer(r'\<(?P<type>\w+):(?P<name>[a-z][a-z0-9_]*)\>', path_pattern):
			url_type = self.url_types[match.group('type')]
			path_placeholders[match.group('name')] = url_type
			path_expression += re.escape(path_pattern[last_pos:match.start(0)]) + '(?P<{name}>{pattern})'.format(name=match.group('name'), pattern=url_type.pattern)
			path_format += path_pattern[last_pos:match.start(0)] + '{{{name}{format}}}'.format(name=match.group('name'), format=url_type.format)
			last_pos = match.end(0)

		path_expression += re.escape(path_pattern[last_pos:])
		path_format += path_pattern[last_pos:]

		return Route(
			path_expression=re.compile("^{}$".format(path_expression)),
			path_format=path_format,
			path_placeholders=path_placeholders,
			**kwargs)

	def match_route(self, path: str) -> Tuple[Optional[Route], Optional[Dict[str,Any]]]:
		for route in self.routes:
			match = re.match(route.path_expression, path)
			if match:
				return route, {name: route.path_placeholders[name].cast(value) for name, value in match.groupdict().items()}
		return None, None

	def url_for(self, name: str, **kwargs) -> str:
		for route in self.routes:
			if route.name == name:
				return route.path_format.format(**kwargs)

	def write_response(self, response: Response, handler: BaseHTTPRequestHandler):
		response.write(handler)

	def run(self, bind=None, port=5000):
		_http_server_test(HandlerClass=partial(RequestHandler, app=self), bind=bind, port=port)


class RequestHandler(BaseHTTPRequestHandler):
	def __init__(self, *args, app=None, **kwargs):
		self.app = app
		super().__init__(*args, **kwargs)

	def handle_one_request(self):
		try:
			self.raw_requestline = self.rfile.readline(65537)

			if not self.raw_requestline:
				self.close_connection = True
				return

			if len(self.raw_requestline) > 65536:
				self.requestline = ''
				self.request_version = ''
				self.command = ''
				self.send_error(HTTPStatus.REQUEST_URI_TOO_LONG)
				return

			if not self.parse_request():
				return

			route, parameters = self.app.match_route(self.path)

			if not route:
				self.send_error(HTTPStatus.NOT_FOUND, "No route found")
				return
			
			if self.command not in route.methods:
				self.send_error(HTTPStatus.NOT_IMPLEMENTED, "Unsupported method (%r)" % self.command)
				return

			try:
				request = Request(self.command, self.path)
				response = route.callback(request, **parameters)
				self.app.write_response(response, self)
				self.wfile.flush() #actually send the response if not already done.
			except Exception as e:
				self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "Error while handling request: {!r}\n\n{}".format(e, _full_stack()))
				return
		except socket.timeout as e:
			self.log_error("Request timed out: %r", e)
			self.close_connection = True
			return


def send_file(filename, **kwargs):
	headers = kwargs.get('headers', {})
	headers['Content-Length'] = os.path.getsize(filename)
	mimetype, encoding = mimetypes.guess_type(filename)
	if mimetype:
		headers['Content-Type'] = mimetype
	if encoding:
		headers['Content-Encoding'] = encoding
	kwargs['headers'] = headers
	return FileResponse(open(filename, 'rb'), **kwargs)


class JSONEncoder(json.JSONEncoder):
	def default(self, data):
		if isinstance(data, frozenset):
			return list(data)
		else:
			return super().default(data)


def send_json(data, **kwargs):
	return Response(json.dumps(data, cls=JSONEncoder), **kwargs)


def main(app):
	import argparse
	parser = argparse.ArgumentParser()
	parser.add_argument('--bind', '-b', metavar='ADDRESS', help='Specify alternate bind address [default: all interfaces]')
	parser.add_argument('port', action='store', default=5000, type=int, nargs='?', help='Specify alternate port [default: 8000]')
	args = parser.parse_args()
	app.run(bind=args.bind, port=args.port)
