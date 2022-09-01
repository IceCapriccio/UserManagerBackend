"""
gpustat.web


MIT License

Copyright (c) 2018-2020 Jongwook Choi (@wookayin)
"""

from typing import List, Tuple, Optional
import os
import sys
import traceback
import urllib
import ssl
import time
import asyncio
import asyncssh
import aiohttp

from datetime import datetime
from collections import OrderedDict, Counter

from termcolor import cprint, colored
from aiohttp import web
import aiohttp_jinja2 as aiojinja2
import threading

__PATH__ = os.path.abspath(os.path.dirname(__file__))

#  this variable will be updated only in gpu_query_start function,
# context.delete_vote will compare with this variable to determine whether to delete a user in context.use_time
# Because only if all hosts' status don't exist this user, in other words, context.delete_vote[user] >= MACHINES_COUNT,
# we can safely delete this user in context.user_time
MACHINES_COUNT = 0


###############################################################################
# Background workers to collect information from nodes
###############################################################################

class Context(object):
    '''The global context object.'''

    def __init__(self):
        self.host_status = OrderedDict()
        self.interval = 5.0
        self.use_time = OrderedDict()
        self.delete_vote = OrderedDict()

    def host_set_message(self, hostname: str, msg: str, ncolor_msg: str = None):
        self.host_status[hostname] = colored(f"({hostname}) ", 'white') + msg + '\n'
        if ncolor_msg is not None:
            print(bytes(ncolor_msg))

    def update_use_time(self, text: str):
        """
        According to non-colored text of gpustat, update every user's use time.

        a non-colored text of gpustat appears like following:
yons-desktop                   Fri Mar 11 14:41:50 2022  465.19.01
[0] NVIDIA GeForce GTX 1080 Ti | 48\'C,   0 % |    81 / 11175 MB | gdm(27M) gdm(50M)
        """
        user_mem_list = text.split('|')[-1].split()  # ['gdm(27M)', 'gdm(50M)']
        user_mem = OrderedDict()
        total_mem = int(text.split('|')[-2].split('/')[-1].strip(' MB'))
        for item in user_mem_list:
            user, mem = item.rstrip('M)').split('(')  # 'gdm', '27'
            mem = int(mem)
            user_mem[user] = user_mem.get(user, 0) + mem

        # update self.user_time
        # Only if all hosts have no userA running program, we can delete userA.
        all_users = set(self.use_time.keys()) | set(user_mem.keys())
        for user in all_users:
            if user in user_mem and user_mem[
                user] > total_mem / 10:  # be regarded as 'occupied' status when memory occupation rate >= 10%
                self.use_time[user] = self.use_time.get(user, 0) + 5
                self.delete_vote[user] = 0
            elif user in self.use_time.keys() and (user not in user_mem.keys() or user_mem[user] < total_mem / 10):
                self.delete_vote[user] = self.delete_vote.get(user, 0) + 1
                if self.delete_vote.get(user, 0) >= 3 and user in self.use_time.keys():
                    del self.use_time[user]


context = Context()


async def run_client(hostname: str, exec_cmd: str, *, port=22, username=None,
                     poll_delay=None, timeout=30.0,
                     name_length=None, verbose=False):
    '''An async handler to collect gpustat through a SSH channel.'''
    L = name_length or 0
    if poll_delay is None:
        poll_delay = context.interval

    async def _loop_body():
        # establish a SSH connection.
        async with asyncssh.connect(hostname, port=port, username=username) as conn:
            cprint(
                f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time()))}[{hostname:<{L}}] SSH connection established!",
                attrs=['bold'])

            while True:
                if False:  # verbose: XXX DEBUG
                    print(f"[{hostname:<{L}}] querying... ")

                result = await asyncio.wait_for(conn.run(exec_cmd), timeout=timeout)

                # Colored text follows some color-control bytes, so request extra non-colored text to update use time

                text_result = await asyncio.wait_for(conn.run(exec_cmd.rstrip('--color')), timeout=timeout)
                now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                if result.exit_status != 0:
                    cprint(f"[{now} [{hostname:<{L}}] Error, exitcode={result.exit_status}", color='red')
                    cprint(result.stderr or '', color='red')
                    stderr_summary = (result.stderr or '').split('\n')[0]
                    context.host_set_message(hostname,
                                             colored(f'[exitcode {result.exit_status}] {stderr_summary}', 'red'))
                else:
                    if verbose:
                        cprint(f"[{now} [{hostname:<{L}}] OK from gpustat ({len(result.stdout)} bytes)", color='cyan')

                    # update data
                    context.host_status[hostname] = result.stdout
                    context.update_use_time(text_result.stdout)
                # wait for a while...
                await asyncio.sleep(poll_delay)

    while True:
        try:
            # start SSH connection, or reconnect if it was disconnected
            await _loop_body()

        except asyncio.CancelledError:
            cprint(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
                   f"[{hostname:<{L}}] Closed as being cancelled.", attrs=['bold'])
            break
        except asyncio.TimeoutError as ex:
            # timeout (retry)
            cprint(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
                   f"Timeout after {timeout} sec: {hostname}", color='red')
            context.host_set_message(hostname, colored(f"Timeout after {timeout} sec", 'red'))
        except (asyncssh.misc.DisconnectError, asyncssh.misc.ChannelOpenError, OSError) as ex:
            # error or disconnected (retry)
            cprint(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
                   f"Disconnected : hostname:{hostname}, port:{port}, username:{username}, {str(ex)}", color='red')
            context.host_set_message(hostname, colored(str(ex), 'red'))
        except Exception as e:
            # A general exception unhandled, throw
            cprint(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
                   f"[{hostname:<{L}}] {e}", color='red')
            context.host_set_message(hostname, colored(f"{type(e).__name__}: {e}", 'red'))
            cprint(traceback.format_exc())
            raise

        # retry upon timeout/disconnected, etc.
        cprint(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
               f"[{hostname:<{L}}] Disconnected, retrying in {poll_delay} sec...", color='yellow')
        await asyncio.sleep(poll_delay)


async def spawn_clients(hosts: List[str], *,
                        default_port: int, verbose=False):
    """Create a set of async handlers, one per host."""

    def _parse_host_string(netloc: str) -> Tuple[str, Optional[str], Optional[int]]:
        """
        Parse a connection string (netloc) in the form of `USERNAME@HOSTNAME[:PORT]`
        and returns (HOSTNAME, USERNAME, PORT).
        """
        pr = urllib.parse.urlparse('ssh://{}/'.format(netloc))
        assert pr.hostname is not None, netloc

        cprint(f'_parse_host_string: ({netloc}) --> '
               f'(hostname: {pr.hostname}, port: {pr.port}, username: {pr.username})', color='green')
        return pr.hostname, pr.username, pr.port

    try:
        host_names, host_users, host_ports = zip(*(_parse_host_string(host) for host in hosts))

        # can't activate conda environment by asyncssh, so specify command for each host
        exec_command_dict = {
            'localhost': '/home/ice/.conda/envs/ice/bin/gpustat --color',
            '127.0.0.1': '/home/ice/.conda/envs/ice/bin/gpustat --color',
            '10.112.118.116': '/home/ice/.conda/envs/ice/bin/gpustat --color',
            '10.126.239.153': '/home/qsjtlb/.conda/envs/ice/bin/gpustat --color',
            '10.126.196.102': '/home/bupt/.conda/envs/bupt/bin/gpustat --color'
        }

        # initial response
        for hostname in host_names:
            context.host_set_message(hostname, "Loading ...")

        name_length = max(len(hostname) for hostname in host_names)

        # launch all clients parallel
        clients = [run_client(hostname, exec_command_dict[hostname], port=port or default_port, username=username,
                              verbose=verbose, name_length=name_length)
                   for (hostname, username, port) in zip(host_names, host_users, host_ports)
                   ]
        await asyncio.gather(*clients)
    except Exception as ex:
        # TODO: throw the exception outside and let aiohttp abort startup
        traceback.print_exc()
        cprint(colored("Error: An exception occurred during the startup.", 'red'))


###############################################################################
# webserver handlers.
###############################################################################

# monkey-patch ansi2html scheme. TODO: better color codes
import ansi2html

scheme = 'solarized'
ansi2html.style.SCHEME[scheme] = list(ansi2html.style.SCHEME[scheme])
ansi2html.style.SCHEME[scheme][0] = '#555555'
ansi_conv = ansi2html.Ansi2HTMLConverter(dark_bg=True, scheme=scheme)


def render_gpustat_body():
    body = ''
    for host, status in context.host_status.items():
        if not status:
            continue
        body += status
    return ansi_conv.convert(body, full=False)


async def homepage_handler(request):
    ''' Renders the html page. '''

    data = dict(
        ansi2html_headers=ansi_conv.produce_headers().replace('\n', ' '),
        http_host=request.host,
        interval=int(context.interval * 1000)
    )
    response = aiojinja2.render_template('gpustat.html', request, data)
    response.headers['Content-Language'] = 'en'
    return response


async def time_use_handler(request):
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
          f"INFO: Websocket of time-use from {request.remote} established")
    wsr = web.WebSocketResponse()
    await wsr.prepare(request)

    async def _handle_websocketmessage(msg):
        if msg.data == 'close':
            await wsr.close()
        else:
            await wsr.send_json(context.use_time)

    async for msg in wsr:
        if msg.type == aiohttp.WSMsgType.CLOSE:
            break
        elif msg.type == aiohttp.WSMsgType.TEXT:
            await _handle_websocketmessage(msg)
        elif msg.type == aiohttp.WSMsgType.ERROR:
            cprint(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
                   f"Websocket connection closed with exception {wsr.exception()}", color='red')

    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
          f"INFO: Websocket of time-use from {request.remote} closed")
    return wsr


async def host_status_handler(request):
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
          f"INFO: Websocket of host status from {request.remote} established")

    wsr = web.WebSocketResponse()
    await wsr.prepare(request)

    async def _handle_websocketmessage(msg):
        if msg.data == 'close':
            await wsr.close()
        else:
            # send the rendered HTML body as a websocket message.
            body = render_gpustat_body()
            await wsr.send_str(body)

    async for msg in wsr:
        if msg.type == aiohttp.WSMsgType.CLOSE:
            break
        elif msg.type == aiohttp.WSMsgType.TEXT:
            await _handle_websocketmessage(msg)
        elif msg.type == aiohttp.WSMsgType.ERROR:
            cprint(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
                   f"Websocket connection closed with exception {wsr.exception()}", color='red')

    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
          f"INFO: Websocket of host status from {request.remote} closed")
    return wsr


###############################################################################
# app factory and entrypoint.
###############################################################################


def create_app(loop, *,
               hosts=['localhost'],
               default_port: int = 22,
               ssl_certfile: Optional[str] = None,
               ssl_keyfile: Optional[str] = None,
               verbose=True):
    app = web.Application()
    app.add_routes([
        web.get('/status', host_status_handler),
        web.get('/time', time_use_handler),
        web.get('/', homepage_handler)
    ])

    async def start_background_tasks(app):
        clients = spawn_clients(hosts, default_port=default_port, verbose=verbose)
        app['tasks'] = app.loop.create_task(clients)
        await asyncio.sleep(0.1)

    app.on_startup.append(start_background_tasks)

    async def shutdown_background_tasks(app):
        cprint(f"... Terminating the tasks: {app['tasks']}", color='yellow')
        app['tasks'].cancel()
        await app['tasks']

    app.on_shutdown.append(shutdown_background_tasks)

    # jinja2 setup
    import jinja2
    aiojinja2.setup(app,
                    loader=jinja2.FileSystemLoader(
                        os.path.join(__PATH__, 'template'))
                    )

    # SSL setup
    if ssl_certfile and ssl_keyfile:
        ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ssl_context.load_cert_chain(certfile=ssl_certfile,
                                    keyfile=ssl_keyfile)

        cprint(f"Using Secure HTTPS (SSL/TLS) server ...", color='green')
    else:
        ssl_context = None  # type: ignore
    return app, ssl_context


def gpu_query_start():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('hosts', nargs='*',
                        help='List of nodes. Syntax: HOSTNAME[:PORT]')
    parser.add_argument('--verbose', action='store_true')
    parser.add_argument('--port', type=int, default=48109,
                        help="Port number the web application will listen to. (Default: 48109)")
    parser.add_argument('--ssh-port', type=int, default=22,
                        help="Default SSH port to establish connection through. (Default: 22)")
    parser.add_argument('--interval', type=float, default=5.0,
                        help="Interval (in seconds) between two consecutive requests.")
    parser.add_argument('--ssl-certfile', type=str, default=None,
                        help="Path to the SSL certificate file (Optional, if want to run HTTPS server)")
    parser.add_argument('--ssl-keyfile', type=str, default=None,
                        help="Path to the SSL private key file (Optional, if want to run HTTPS server)")
    parser.add_argument('--exec', type=str,
                        default=None,
                        help="command-line to execute (e.g. gpustat --color --gpuname-width 25)")
    args = parser.parse_args()

    hosts = args.hosts or ['localhost']
    cprint(f"Hosts : {hosts}")
    MACHINES_COUNT = len(hosts)
    if args.interval > 0.1:
        context.interval = args.interval

    loop = asyncio.get_event_loop()
    app, ssl_context = create_app(
        loop, hosts=hosts, default_port=args.ssh_port,
        ssl_certfile=args.ssl_certfile, ssl_keyfile=args.ssl_keyfile,
        verbose=args.verbose)
    web.run_app(app, host='0.0.0.0', port=args.port,
                ssl_context=ssl_context)


if __name__ == '__main__':
    gpu_query_start()
