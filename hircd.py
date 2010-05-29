#!/usr/bin/python

# 
# Very simple hacky ugly IRC server. It doesn't support much other than
# connecting, setting a nickname (partly implemented) and joining a channel.
#
# Most notable is the handling of messages to channels.. everything is
# broadcasted to all channels regardless of the channel you're in. YMMV
#
# Bugs in Kip MAY actually be the result of this IRC server.
#
# Known missing features which might be implemented one day:
#
# - No user list on joining a channel.
# - Proper nick name support is lacking (changing nick party working).
# - No part support.
# - starting server when already started doesn't work properly. PID file is not changed, no error messsage is displayed.
# 
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation
# files (the "Software"), to deal in the Software without
# restriction, including without limitation the rights to use,
# copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the
# Software is furnished to do so, subject to the following
# conditions:
# 
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
# 
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES
# OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
# HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
# WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
# FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
# OTHER DEALINGS IN THE SOFTWARE.
# 

import sys
import optparse
import logging
import ConfigParser
import os
import SocketServer
import socket
import select
import re

RPL_WELCOME          = '001'
ERR_NOSUCHNICK       = '401'
ERR_NOSUCHCHANNEL    = '403'
ERR_ERRONEUSNICKNAME = '432'
ERR_NICKNAMEINUSE    = '433'

class IRCError(Exception):
	"""
	Exception thrown by IRC command handlers to notify client of a server/client error.
	"""
	def __init__(self, code, value):
		self.code = code
		self.value = value

	def __str__(self):
		return repr(self.value)

class IRCChannel(object):
	"""
	Object representing an IRC channel.
	"""
	def __init__(self, name):
		self.name = name
		self.clients = set()

class IRCClient(SocketServer.BaseRequestHandler):
	"""
	IRC client connect and command handling. Client connection is handled by
	the `handle` method which sets up a two-way communication with the client.
	It then handles commands sent by the client by dispatching them to the
	handle_ methods.
	"""
	def __init__(self, request, client_address, server):
		self.user = None
		self.host = client_address  # Client's hostname / ip.
		self.realname = None        # Client's real name
		self.nick = None            # Client's currently registered nickname
		self.send_queue = []        # Messages to send to client (strings)
		self.channels = set()       # Channels the client is in

		SocketServer.BaseRequestHandler.__init__(self, request, client_address, server)

	def handle(self):
		logging.info('Client connected: %s' % (self.client_ident(), ))

		while True:
			buf = ''
			ready_to_read, ready_to_write, in_error = select.select([self.request], [], [], 0.1)

			# Write any commands to the client
			while self.send_queue:
				msg = self.send_queue.pop(0)
				logging.debug('to %s: %s' % (self.client_ident(), msg))
				self.request.send(msg + '\n')

			# See if the client has any commands for us.
			if len(ready_to_read) == 1 and ready_to_read[0] == self.request:
				data = self.request.recv(1024)

				if not data:
					break
				elif len(data) > 0:
					# There is data. Process it and turn it into line-oriented input.
					buf += str(data)

					while buf.find("\n") != -1:
						line, buf = buf.split("\n", 1)
						line = line.rstrip()

						response = ''
						try:
							logging.debug('from %s: %s' % (self.client_ident(), line))
							if ' ' in line:
								command, params = line.split(' ', 1)
							else:
								command = line
								params = ''
							handler = getattr(self, 'handle_%s' % (command.lower()), None)
							if not handler:
								print "No handler for command" # FIXME: raise an error here.
								break
							response = handler(params)
						except AttributeError, e:
							raise e
							logging.error('%s' % (e))
						except IRCError, e:
							response = ':%s %s %s' % (self.server.servername, e.code, e.value)
							logging.error('%s' % (response))
						except Exception, e:
							response = ':%s ERROR %s' % (self.server.servername, repr(e))
							logging.error('%s' % (response))
							raise

						if response:
							logging.debug('to %s: %s' % (self.client_ident(), response))
							self.request.send(response + '\r\n')

		logging.info('Client disconnected: %s' % (self.client_ident()))
		self.request.close()

	def handle_nick(self, params):
		"""
		Handle the iniital setting of the user's nickname and nick changes.
		"""
		nick = params

		# Valid nickname?
		if re.search('[^a-zA-Z0-9\-\[\]\'`^{}_]', nick):
			raise IRCError(ERR_ERRONEUSNICKNAME, ':%s' % (nick))

		if not self.nick:
			# New connection
			if nick in self.server.clients:
				# Someone else is using the nick
				raise IRCError(ERR_NICKNAMEINUSE, 'NICK :%s' % (nick))
			else:
				# Nick is available, register and send welcome
				self.nick = nick
				self.server.clients[nick] = self
				response = ':%s %s %s :Welcome to the ugliest IRC server in the world.' % (self.server.servername, RPL_WELCOME, self.nick)
				return(response)
		else:
			if self.server.clients.get(nick, None) == self:
				# Already registered to user
				return
			elif nick in self.server.clients:
				# Someone else is using the nick
				raise IRCError(ERR_NICKNAMEINUSE, 'NICK :%s' % (nick))
			else:
				# Nick is available. Change the nick.
				message = ':%s NICK :%s' % (self.client_ident(), nick)

				self.server.clients.pop(self.nick)
				prev_nick = self.nick
				self.nick = nick
				self.server.clients[self.nick] = self

				# Send a notification of the nick change to all the clients in
				# the channels the client is in.
				for channel in self.channels:
					for client in channel.clients:
						if client != self: # do not send to client itself.
							client.send_queue.append(message)

				# Send a notification of the nick change to the client itself
				return(message)

	def handle_user(self, params):
		"""
		Handle the USER command which identifies the user to the server.
		"""
		user, mode, unused, realname = params.split(' ', 3)
		self.user = user
		self.mode = mode
		self.realname = realname

	def handle_ping(self, params):
		"""
		Handle client PING requests to keep the connection alive.
		"""
		response = ':%s PONG :%s' % (self.server.servername, self.server.servername)
		return (response)

	def handle_join(self, params):
		"""
		Handle the JOINing of a user to a channel. Valid channel names start
		with a # and consist of a-z, A-Z, 0-9 and/or '_'.
		"""
		channel_name = params

		# Valid channel name?
		if not re.match('^#([a-zA-Z0-9_])+$', channel_name):
			raise IRCError(ERR_NOSUCHCHANNEL, ':%s' % (channel_name))

		# Add user to the channel (create new channel if not exists)
		channel = self.server.channels.setdefault(channel_name, IRCChannel(channel_name))
		channel.clients.add(self)

		# Add channel to user's channel list
		self.channels.add(channel)

		# Send join message to everybody in the channel, including yourself
		response = ':%s JOIN :%s' % (self.client_ident(), channel_name)
		for client in channel.clients:
			client.send_queue.append(response)

	def handle_privmsg(self, params):
		"""
		Handle sending a private message to a user or channel.
		"""
		target, msg = params.split(' ', 1)

		message = ':%s PRIVMSG %s %s' % (self.client_ident(), target, msg)
		if target.startswith('#') or target.startswith('$'):
			# Message to channel
			channel = self.server.channels.get(target)
			if channel:
				for client in channel.clients:
					if client != self:
						client.send_queue.append(message)
			else:
				raise IRCError(ERR_NOSUCHNICK, 'PRIVMSG :%s' % (target))
		else:
			# Message to user
			client = self.server.clients.get(target, None)
			if client:
				client.send_queue.append(message)
			else:
				raise IRCError(ERR_NOSUCHNICK, 'PRIVMSG :%s' % (target))

	def handle_quit(self, params):
		"""
		Handle the client breaking off the connection with a QUIT command.
		"""
		response = ':%s QUIT :%s' % (self.client_ident(), params.lstrip(':'))
		for channel in self.channels:
			for client in channel.clients:
				client.send_queue.append(response)
		self.server.clients.pop(self.nick)

	def client_ident(self):
		"""
		Return the client identifier as included in many command replies.
		"""
		return('%s!%s@%s' % (self.nick, self.user, self.server.servername))

	def finish(self):
		pass

class IRCServer(SocketServer.ThreadingMixIn, SocketServer.TCPServer):
	daemon_threads = True
	allow_reuse_address = True

	def __init__(self, server_address, RequestHandlerClass):
		self.servername = 'localhost'
		self.channels = {} # Existing channels (IRCChannel instances) by channelname
		self.clients = {}  # Connected clients (IRCClient instances) by nickname
		SocketServer.TCPServer.__init__(self, server_address, RequestHandlerClass)

class Daemon:
    """
	Daemonize the current process (detach it from the console).
    """

    def __init__(self):
		# Fork a child and end the parent (detach from parent)
		try:
			pid = os.fork()
			if pid > 0:
				sys.exit(0) # End parent
		except OSError, e:
			sys.stderr.write("fork #1 failed: %d (%s)\n" % (e.errno, e.strerror))
			sys.exit(-2)

		# Change some defaults so the daemon doesn't tie up dirs, etc.
		os.setsid()
		os.umask(0)

		# Fork a child and end parent (so init now owns process)
		try:
			pid = os.fork()
			if pid > 0:
				try:
					f = file('hircd.pid', 'w')
					f.write(str(pid))
					f.close()
				except IOError, e:
					logging.error(e)
					sys.stderr.write(repr(e))
				sys.exit(0) # End parent
		except OSError, e:
			sys.stderr.write("fork #2 failed: %d (%s)\n" % (e.errno, e.strerror))
			sys.exit(-2)

		# Close STDIN, STDOUT and STDERR so we don't tie up the controlling
		# terminal
		for fd in (0, 1, 2):
			try:
				os.close(fd)
			except OSError:
				pass

if __name__ == "__main__":
	#
	# Parameter parsing
	#
	parser = optparse.OptionParser()
	parser.set_usage(sys.argv[0] + " [option]")

	parser.add_option("--start", dest="start", action="store_true", default=True, help="Start hircd (default)")
	parser.add_option("--stop", dest="stop", action="store_true", default=False, help="Stop hircd")
	parser.add_option("--restart", dest="restart", action="store_true", default=False, help="Restart hircd")
	parser.add_option("-a", "--address", dest="listen_address", action="store", default='127.0.0.1', help="IP to listen on")
	parser.add_option("-p", "--port", dest="listen_port", action="store", default='6667', help="Port to listen on")
	parser.add_option("-V", "--verbose", dest="verbose", action="store_true", default=False, help="Be verbose (show lots of output)")
	parser.add_option("-l", "--log-stdout", dest="log_stdout", action="store_true", default=False, help="Also log to stdout")
	parser.add_option("-e", "--errors", dest="errors", action="store_true", default=False, help="Do not intercept errors.")
	parser.add_option("-f", "--foreground", dest="foreground", action="store_true", default=False, help="Do not go into daemon mode.")

	(options, args) = parser.parse_args()

	# Paths
	configfile = os.path.join(os.path.realpath(os.path.dirname(sys.argv[0])),'hircd.ini')
	logfile = os.path.join(os.path.realpath(os.path.dirname(sys.argv[0])),'hircd.log')

	# 
	# Logging
	#
	if options.verbose:
		loglevel = logging.DEBUG
	else:
		loglevel = logging.WARNING

	log = logging.basicConfig(
		level=loglevel,
		format='%(asctime)s:%(levelname)s:%(message)s',
		filename=logfile,
		filemode='a')

	#
	# Handle start/stop/restart commands.
	#
	if options.stop or options.restart:
		pid = None
		try:
			f = file('hircd.pid', 'r')
			pid = int(f.readline())
			f.close()
			os.unlink('hircd.pid')
		except ValueError, e:
			sys.stderr.write('Error in pid file `hircd.pid`. Aborting\n')
			sys.exit(-1)
		except IOError, e:
			pass

		if pid:
			os.kill(pid, 15)
		else:
			sys.stderr.write('hircd not running or no PID file found\n')

		if not options.restart:
			sys.exit(0)

	logging.info("Starting hircd")
	logging.debug("configfile = %s" % (configfile))
	logging.debug("logfile = %s" % (logfile))

	if options.log_stdout:
		console = logging.StreamHandler()
		formatter = logging.Formatter('[%(levelname)s] %(message)s')
		console.setFormatter(formatter)
		console.setLevel(logging.DEBUG)
		logging.getLogger('').addHandler(console)

	if options.verbose:
		logging.info("We're being verbose")

	#
	# Go into daemon mode
	#
	if not options.foreground:
		Daemon()

	#
	# Start server
	#
	try:
		ircserver = IRCServer((options.listen_address, int(options.listen_port)), IRCClient)
		logging.info('Starting hircd on %s:%s' % (options.listen_address, options.listen_port))
		ircserver.serve_forever()
	except socket.error, e:
		logging.error(repr(e))
		sys.exit(-2)
