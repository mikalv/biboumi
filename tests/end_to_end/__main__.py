#!/usr/bin/env python3

import collections
import lxml.etree
import datetime
import slixmpp
import asyncio
import logging
import signal
import atexit
import time
import sys
import io
import os
from functools import partial
from slixmpp.xmlstream.matcher.base import MatcherBase

if not hasattr(asyncio, "ensure_future"):
    asyncio.ensure_future = getattr(asyncio, "async")

class MatchAll(MatcherBase):
    """match everything"""

    def match(self, xml):
        return True


class StanzaError(Exception):
    """
    Raised when a step fails.
    """
    pass


class SkipStepError(Exception):
    """
    Raised by a step when it needs to be skiped, by running
    the next available step immediately.
    """
    pass


class XMPPComponent(slixmpp.BaseXMPP):
    """
    XMPPComponent sending a “scenario” of stanzas, checking that the responses
    match the expected results.
    """

    def __init__(self, scenario, biboumi):
        super().__init__(jid="biboumi.localhost", default_ns="jabber:component:accept")
        self.is_component = True
        self.auto_authorize = None # Do not accept or reject subscribe requests automatically
        self.auto_subscribe = False
        self.stream_header = '<stream:stream %s %s from="%s" id="%s">' % (
            'xmlns="jabber:component:accept"',
            'xmlns:stream="%s"' % self.stream_ns,
            self.boundjid, self.get_id())
        self.stream_footer = "</stream:stream>"

        self.register_handler(slixmpp.Callback('Match All',
                                               MatchAll(None),
                                               self.handle_incoming_stanza))

        self.add_event_handler("session_end", self.on_end_session)

        asyncio.ensure_future(self.accept_routine())

        self.scenario = scenario
        self.biboumi = biboumi
        # A callable, taking a stanza as argument and raising a StanzaError
        # exception if the test should fail.
        self.stanza_checker = None
        self.failed = False
        self.accepting_server = None

        self.saved_values = {}

    def error(self, message):
        print("[31;1mFailure[0m: %s" % (message,))
        self.scenario.steps = []
        self.failed = True

    def on_end_session(self, _):
        self.loop.stop()

    def handle_incoming_stanza(self, stanza):
        if self.stanza_checker:
            try:
                self.stanza_checker(stanza)
            except StanzaError as e:
                self.error(e)
            except SkipStepError:
                # Run the next step and then re-handle this same stanza
                self.run_scenario()
                return self.handle_incoming_stanza(stanza)
            self.stanza_checker = None
        self.run_scenario()

    def run_scenario(self):
        if self.scenario.steps:
            step = self.scenario.steps.pop(0)
            try:
                step(self, self.biboumi)
            except Exception as e:
                self.error(e)
                self.run_scenario()
        else:
            if self.biboumi:
                self.biboumi.stop()

    @asyncio.coroutine
    def accept_routine(self):
        self.accepting_server = yield from self.loop.create_server(lambda: self,
                                                                   "127.0.0.1", 8811, reuse_address=True)

    def check_stanza_against_all_expected_xpaths(self):
        pass


def match(stanza, xpath):
    tree = lxml.etree.parse(io.StringIO(str(stanza)))
    matched = tree.xpath(xpath, namespaces={'re': 'http://exslt.org/regular-expressions',
                                            'muc_user': 'http://jabber.org/protocol/muc#user',
                                            'muc_owner': 'http://jabber.org/protocol/muc#owner',
                                            'muc': 'http://jabber.org/protocol/muc',
                                            'disco_info': 'http://jabber.org/protocol/disco#info',
                                            'muc_traffic': 'http://jabber.org/protocol/muc#traffic',
                                            'disco_items': 'http://jabber.org/protocol/disco#items',
                                            'commands': 'http://jabber.org/protocol/commands',
                                            'dataform': 'jabber:x:data',
                                            'version': 'jabber:iq:version',
                                            'mam': 'urn:xmpp:mam:2',
                                            'rms': 'http://jabber.org/protocol/rsm',
                                            'delay': 'urn:xmpp:delay',
                                            'forward': 'urn:xmpp:forward:0',
                                            'client': 'jabber:client',
                                            'rsm': 'http://jabber.org/protocol/rsm',
                                            'carbon': 'urn:xmpp:carbons:2',
                                            'hints': 'urn:xmpp:hints',
                                            'stanza': 'urn:ietf:params:xml:ns:xmpp-stanzas',
                                            'stable_id': 'urn:xmpp:sid:0'})
    return matched


def check_xpath(xpaths, xmpp, after, stanza):
    for xpath in xpaths:
        expected = True
        real_xpath = xpath
        # We can check that a stanza DOESN’T match, by adding a ! before it.
        if xpath.startswith('!'):
            expected = False
            xpath = xpath[1:]
        matched = match(stanza, xpath)
        if (expected and not matched) or (not expected and matched):
            raise StanzaError("Received stanza\n%s\ndid not match expected xpath\n%s" % (stanza, real_xpath))
    if after:
        if isinstance(after, collections.Iterable):
            for af in after:
                af(stanza, xmpp)
        else:
            after(stanza, xmpp)

def all_xpaths_match(stanza, xpaths):
    for xpath in xpaths:
        matched = match(stanza, xpath)
        if not matched:
            return False
    return True

def check_list_of_xpath(list_of_xpaths, xmpp, stanza):
    found = None
    for i, xpaths in enumerate(list_of_xpaths):
        if all_xpaths_match(stanza, xpaths):
            found = True
            list_of_xpaths.pop(i)
            break

    if not found:
        raise StanzaError("Received stanza “%s” did not match any of the expected xpaths:\n%s" % (stanza, list_of_xpaths))

    if list_of_xpaths:
        step = partial(expect_unordered_already_formatted, list_of_xpaths)
        xmpp.scenario.steps.insert(0, step)


def check_xpath_optional(xpaths, xmpp, after, stanza):
    try:
        check_xpath(xpaths, xmpp, after, stanza)
    except StanzaError:
        raise SkipStepError()


class Scenario:
    """Defines a list of actions that are executed in sequence, until one of
    them throws an exception, or until the end.  An action can be something
    like “send a stanza”, “receive the next stanza and check that it matches
    the given XPath”, “send a signal”, “wait for the end of the process”,
    etc
    """

    def __init__(self, name, steps, conf="basic"):
        """
        Steps is a list of 2-tuple:
        [(action, answer), (action, answer)]
        """
        self.name = name
        self.steps = []
        self.conf = conf
        for elem in steps:
            if isinstance(elem, collections.Iterable):
                for step in elem:
                    self.steps.append(step)
            else:
                self.steps.append(elem)


class ProcessRunner:
    def __init__(self):
        self.process = None
        self.signal_sent = False
        self.create = None

    @asyncio.coroutine
    def start(self):
        self.process = yield from self.create

    @asyncio.coroutine
    def wait(self):
        code = yield from self.process.wait()
        return code

    def stop(self):
        if not self.signal_sent:
            self.signal_sent = True
            if self.process:
                self.process.send_signal(signal.SIGINT)

    def __del__(self):
        self.stop()


class BiboumiRunner(ProcessRunner):
    def __init__(self, name):
        super().__init__()
        self.name = name
        self.fd = open("biboumi_%s_output.txt" % (name,), "w")
        with_valgrind = os.environ.get("E2E_WITH_VALGRIND") is not None
        if with_valgrind:
            self.create = asyncio.create_subprocess_exec("valgrind", "--suppressions=" + (os.environ.get("E2E_BIBOUMI_SUPP_DIR") or "") + "biboumi.supp", "--leak-check=full", "--show-leak-kinds=all",
                                                         "--errors-for-leak-kinds=all", "--error-exitcode=16",
                                                         "./biboumi", "test.conf", stdin=None, stdout=self.fd,
                                                         stderr=self.fd, loop=None, limit=None)
        else:
            self.create = asyncio.create_subprocess_exec("./biboumi", "test.conf", stdin=None, stdout=self.fd,
                                                         stderr=self.fd, loop=None, limit=None)


class IrcServerRunner(ProcessRunner):
    def __init__(self):
        super().__init__()
        self.create = asyncio.create_subprocess_exec("charybdis", "-foreground", "-configfile", os.getcwd() + "/../tests/end_to_end/ircd.conf",
                                                     stderr=asyncio.subprocess.PIPE)


def send_stanza(stanza, xmpp, biboumi):
    replacements = common_replacements
    replacements.update(xmpp.saved_values)
    xmpp.send_raw(stanza.format_map(replacements))
    asyncio.get_event_loop().call_soon(xmpp.run_scenario)


def expect_stanza(xpaths, xmpp, biboumi, optional=False, after=None):
    replacements = common_replacements
    replacements.update(xmpp.saved_values)
    check_func = check_xpath if not optional else check_xpath_optional
    if isinstance(xpaths, str):
        xmpp.stanza_checker = partial(check_func, [xpaths.format_map(replacements)], xmpp, after)
    elif isinstance(xpaths, tuple):
        xmpp.stanza_checker = partial(check_func, [xpath.format_map(replacements) for xpath in xpaths], xmpp, after)
    else:
        print("Warning, from argument type passed to expect_stanza: %s" % (type(xpaths)))

def save_current_timestamp_plus_delta(key, delta, message, xmpp):
    now_plus_delta = datetime.datetime.utcnow() + delta
    xmpp.saved_values[key] = now_plus_delta.strftime("%FT%T.967Z")

def sleep_for(duration, xmpp, biboumi):
    time.sleep(duration)
    asyncio.get_event_loop().call_soon(xmpp.run_scenario)

# list_of_xpaths: [(xpath, xpath), (xpath, xpath), (xpath)]
def expect_unordered(list_of_xpaths, xmpp, biboumi):
    formatted_list_of_xpaths = []
    for xpaths in list_of_xpaths:
        formatted_xpaths = []
        for xpath in xpaths:
            formatted_xpath = xpath.format_map(common_replacements)
            formatted_xpaths.append(formatted_xpath)
        formatted_list_of_xpaths.append(tuple(formatted_xpaths))
    expect_unordered_already_formatted(formatted_list_of_xpaths, xmpp, biboumi)

def expect_unordered_already_formatted(formatted_list_of_xpaths, xmpp, biboumi):
    xmpp.stanza_checker = partial(check_list_of_xpath, formatted_list_of_xpaths, xmpp)


class BiboumiTest:
    """
    Spawns a biboumi process and a fake XMPP Component that will run a
    Scenario.  It redirects the outputs of the subprocess into separated
    files, and detects any failure in the running of the scenario.
    """

    def __init__(self, scenario, expected_code=0):
        self.scenario = scenario
        self.expected_code = expected_code

    def run(self):
        with_valgrind = os.environ.get("E2E_WITH_VALGRIND") is not None
        print("Running scenario: [33;1m%s[0m%s" % (self.scenario.name, " (with valgrind)" if with_valgrind else ''))
        # Redirect the slixmpp logging into a specific file
        output_filename = "slixmpp_%s_output.txt" % (self.scenario.name,)
        with open(output_filename, "w"):
            pass
        logging.basicConfig(level=logging.DEBUG,
                            format='%(levelname)-8s %(message)s',
                            filename=output_filename)

        with open("test.conf", "w") as fd:
            fd.write(confs[self.scenario.conf])

        try:
            os.remove("e2e_test.sqlite")
        except FileNotFoundError:
            pass

        start_datetime = datetime.datetime.now()

        # Start the XMPP component and biboumi
        biboumi = BiboumiRunner(self.scenario.name)
        xmpp = XMPPComponent(self.scenario, biboumi)
        asyncio.get_event_loop().run_until_complete(biboumi.start())

        asyncio.get_event_loop().call_soon(xmpp.run_scenario)

        xmpp.process()
        code = asyncio.get_event_loop().run_until_complete(biboumi.wait())
        xmpp.biboumi = None
        self.scenario.steps.clear()

        delta = datetime.datetime.now() - start_datetime

        failed = False
        if not xmpp.failed:
            if code != self.expected_code:
                xmpp.error("Wrong return code from biboumi's process: %d" % (code,))
                failed = True
            else:
                print("[32;1mSuccess![0m ({}s)".format(round(delta.total_seconds(), 2)))
        else:
            failed = True

        xmpp.saved_values.clear()

        if xmpp.server:
            xmpp.accepting_server.close()

        return not failed


confs = {
'basic':
"""hostname=biboumi.localhost
password=coucou
db_name=e2e_test.sqlite
port=8811
admin=admin@example.com
identd_port=1113
outgoing_bind=127.0.0.1""",

'fixed_server':
"""hostname=biboumi.localhost
password=coucou
db_name=e2e_test.sqlite
port=8811
fixed_irc_server=irc.localhost
admin=admin@example.com
identd_port=1113
""",

'persistent_by_default':
"""hostname=biboumi.localhost
password=coucou
db_name=e2e_test.sqlite
port=8811
persistent_by_default=true
""",}

common_replacements = {
    'irc_server_one': 'irc.localhost@biboumi.localhost',
    'irc_server_two': 'localhost@biboumi.localhost',
    'irc_host_one': 'irc.localhost',
    'irc_host_two': 'localhost',
    'biboumi_host': 'biboumi.localhost',
    'resource_one': 'resource1',
    'resource_two': 'resource2',
    'nick_one': 'Nick',
    'jid_one': 'first@example.com',
    'jid_two': 'second@example.com',
    'jid_admin': 'admin@example.com',
    'nick_two': 'Bobby',
    'nick_three': 'Bernard',
    'lower_nick_one': 'nick',
    'lower_nick_two': 'bobby',
}


def handshake_sequence():
    return (partial(expect_stanza, "//handshake"),
            partial(send_stanza, "<handshake xmlns='jabber:component:accept'/>"))


def connection_begin_sequence(irc_host, jid, expected_irc_presence=False, fixed_irc_server=False):
    jid = jid.format_map(common_replacements)
    if fixed_irc_server:
        xpath    = "/message[@to='" + jid + "'][@from='biboumi.localhost']/body[text()='%s']"
        xpath_re = "/message[@to='" + jid + "'][@from='biboumi.localhost']/body[re:test(text(), '%s')]"
    else:
        xpath    = "/message[@to='" + jid + "'][@from='" + irc_host + "@biboumi.localhost']/body[text()='%s']"
        xpath_re = "/message[@to='" + jid + "'][@from='" + irc_host + "@biboumi.localhost']/body[re:test(text(), '%s')]"
    result = (
    partial(expect_stanza,
            (xpath % ('Connecting to %s:6697 (encrypted)' % irc_host),
            "/message/hints:no-copy",
            "/message/carbon:private"
            )
           ),
    partial(expect_stanza,
            xpath % 'Connection failed: Connection refused'),
    partial(expect_stanza,
            xpath % ('Connecting to %s:6670 (encrypted)' % irc_host)),
    partial(expect_stanza,
            xpath % 'Connection failed: Connection refused'),
    partial(expect_stanza,
            xpath % ('Connecting to %s:6667 (not encrypted)' % irc_host)),
    partial(expect_stanza,
            xpath % 'Connected to IRC server.'))

    if expected_irc_presence:
        result += (partial(expect_stanza, "/presence[@from='" + irc_host + "@biboumi.localhost']"),)

    # These five messages can be receive in any order
    result += (
    partial(expect_stanza,
            xpath_re % (r'^%s: (\*\*\* Checking Ident|\*\*\* Looking up your hostname\.\.\.|\*\*\* Found your hostname: .*|ACK multi-prefix|\*\*\* Got Ident response)$' % 'irc.localhost')),
    partial(expect_stanza,
            xpath_re % (r'^%s: (\*\*\* Checking Ident|\*\*\* Looking up your hostname\.\.\.|\*\*\* Found your hostname: .*|ACK multi-prefix|\*\*\* Got Ident response)$' % 'irc.localhost')),
    partial(expect_stanza,
            xpath_re % (r'^%s: (\*\*\* Checking Ident|\*\*\* Looking up your hostname\.\.\.|\*\*\* Found your hostname: .*|ACK multi-prefix|\*\*\* Got Ident response)$' % 'irc.localhost')),
    partial(expect_stanza,
            xpath_re % (r'^%s: (\*\*\* Checking Ident|\*\*\* Looking up your hostname\.\.\.|\*\*\* Found your hostname: .*|ACK multi-prefix|\*\*\* Got Ident response)$' % 'irc.localhost')),
    partial(expect_stanza,
            xpath_re % (r'^%s: (\*\*\* Checking Ident|\*\*\* Looking up your hostname\.\.\.|\*\*\* Found your hostname: .*|ACK multi-prefix|\*\*\* Got Ident response)$' % 'irc.localhost')),
    )

    return result

def connection_tls_begin_sequence(irc_host, jid, fixed_irc_server):
    jid = jid.format_map(common_replacements)
    if fixed_irc_server:
        xpath    = "/message[@to='" + jid + "'][@from='biboumi.localhost']/body[text()='%s']"
        xpath_re = "/message[@to='" + jid + "'][@from='biboumi.localhost']/body[re:test(text(), '%s')]"
    else:
        xpath    = "/message[@to='" + jid + "'][@from='" + irc_host + "@biboumi.localhost']/body[text()='%s']"
        xpath_re = "/message[@to='" + jid + "'][@from='" + irc_host + "@biboumi.localhost']/body[re:test(text(), '%s')]"
    irc_host = 'irc.localhost'
    return (
        partial(expect_stanza,
                (xpath % ('Connecting to %s:7778 (encrypted)' % irc_host),
                 "/message/hints:no-copy",
                 "/message/carbon:private",
                )
               ),
        partial(expect_stanza,
                xpath % 'Connected to IRC server (encrypted).'),
        # These five messages can be receive in any order
        partial(expect_stanza,
                xpath_re % (r'^%s: (\*\*\* Checking Ident|\*\*\* Looking up your hostname\.\.\.|\*\*\* Found your hostname: .*|ACK multi-prefix|\*\*\* Got Ident response)$' % irc_host)),
        partial(expect_stanza,
                xpath_re % (r'^%s: (\*\*\* Checking Ident|\*\*\* Looking up your hostname\.\.\.|\*\*\* Found your hostname: .*|ACK multi-prefix|\*\*\* Got Ident response)$' % irc_host)),
        partial(expect_stanza,
                xpath_re % (r'^%s: (\*\*\* Checking Ident|\*\*\* Looking up your hostname\.\.\.|\*\*\* Found your hostname: .*|ACK multi-prefix|\*\*\* Got Ident response)$' % irc_host)),
        partial(expect_stanza,
                xpath_re % (r'^%s: (\*\*\* Checking Ident|\*\*\* Looking up your hostname\.\.\.|\*\*\* Found your hostname: .*|ACK multi-prefix|\*\*\* Got Ident response)$' % irc_host)),
        partial(expect_stanza,
                xpath_re % (r'^%s: (\*\*\* Checking Ident|\*\*\* Looking up your hostname\.\.\.|\*\*\* Found your hostname: .*|ACK multi-prefix|\*\*\* Got Ident response)$' % irc_host)),
    )

def connection_end_sequence(irc_host, jid, fixed_irc_server=False):
    jid = jid.format_map(common_replacements)
    if fixed_irc_server:
        xpath    = "/message[@to='" + jid + "'][@from='biboumi.localhost']/body[text()='%s']"
        xpath_re = "/message[@to='" + jid + "'][@from='biboumi.localhost']/body[re:test(text(), '%s')]"
    else:
        xpath    = "/message[@to='" + jid + "'][@from='" + irc_host + "@biboumi.localhost']/body[text()='%s']"
        xpath_re = "/message[@to='" + jid + "'][@from='" + irc_host + "@biboumi.localhost']/body[re:test(text(), '%s')]"
    irc_host = 'irc.localhost'
    return (
    partial(expect_stanza,
            xpath_re % (r'^%s: Your host is .*$' % irc_host)),
    partial(expect_stanza,
            xpath_re % (r'^%s: This server was created .*$' % irc_host)),
    partial(expect_stanza,
            xpath_re % (r'^%s: There are \d+ users and \d+ invisible on \d+ servers$' % irc_host)),
    partial(expect_stanza,
            xpath_re % (r'^%s: \d+ unknown connection\(s\)$' % irc_host), optional=True),
    partial(expect_stanza,
            xpath_re % (r'^%s: \d+ channels formed$' % irc_host), optional=True),
    partial(expect_stanza,
            xpath_re % (r'^%s: I have \d+ clients and \d+ servers$' % irc_host)),
    partial(expect_stanza,
            xpath_re % (r'^%s: \d+ \d+ Current local users \d+, max \d+$' % irc_host)),
    partial(expect_stanza,
            xpath_re % (r'^%s: \d+ \d+ Current global users \d+, max \d+$' % irc_host)),
    partial(expect_stanza,
            xpath_re % (r'^%s: Highest connection count: \d+ \(\d+ clients\) \(\d+ connections received\)$' % irc_host)),
    partial(expect_stanza,
            xpath % "- This is charybdis MOTD you might replace it, but if not your friends will\n- laugh at you.\n"),
    partial(expect_stanza,
            xpath_re % r'^User mode for \w+ is \[\+Z?i\]$'),
    )

def connection_middle_sequence(irc_host, jid, fixed_irc_server=False):
    if fixed_irc_server:
        xpath_re = "/message[@to='" + jid + "'][@from='biboumi.localhost']/body[re:test(text(), '%s')]"
    else:
        xpath_re = "/message[@to='" + jid + "'][@from='" + irc_host + "@biboumi.localhost']/body[re:test(text(), '%s')]"
    irc_host = 'irc.localhost'
    return (
        partial(expect_stanza, xpath_re % (r'^%s: \*\*\* You are exempt from flood limits$' % irc_host)),
    )


def connection_sequence(irc_host, jid, expected_irc_presence=False, fixed_irc_server=False):
    return connection_begin_sequence(irc_host, jid, expected_irc_presence, fixed_irc_server=fixed_irc_server) +\
           connection_middle_sequence(irc_host, jid, fixed_irc_server=fixed_irc_server) +\
           connection_end_sequence(irc_host, jid, fixed_irc_server=fixed_irc_server)

def connection_tls_sequence(irc_host, jid, fixed_irc_server=False):
    return connection_tls_begin_sequence(irc_host, jid, fixed_irc_server) + \
           connection_middle_sequence(irc_host, jid, fixed_irc_server) +\
           connection_end_sequence(irc_host, jid, fixed_irc_server)


def extract_attribute(xpath, name, stanza):
    matched = match(stanza, xpath)
    return matched[0].get(name)

def chan_name_from_jid(jid):
    return jid[1:jid.find('%')]


def extract_text(xpath, stanza):
    matched = match(stanza, xpath)
    return matched[0].text

def save_value(name, func, stanza, xmpp):
    xmpp.saved_values[name] = func(stanza)


if __name__ == '__main__':

    atexit.register(asyncio.get_event_loop().close)

    # Start the test component, accepting connections on the configured
    # port.
    scenarios = (
        Scenario("basic_handshake_success",
                 [
                     handshake_sequence()
                 ]),
        Scenario("irc_server_connection",
                 [
                     handshake_sequence(),
                     partial(send_stanza,
                             "<presence from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}/{nick_one}' />"),
                     connection_sequence("irc.localhost", '{jid_one}/{resource_one}'),
                 ]),
        Scenario("irc_server_connection_failure",
                 [
                     handshake_sequence(),
                     partial(send_stanza,
                             "<presence from='{jid_one}/{resource_one}' to='#foo%doesnotexist@{biboumi_host}/{nick_one}' />"),
                     partial(expect_stanza,
                             "/message/body[text()='Connecting to doesnotexist:6697 (encrypted)']"),
                     partial(expect_stanza,
                             "/message/body[re:test(text(), 'Connection failed: (Domain name not found|Name or service not known)')]"),
                     partial(expect_stanza,
                             ("/presence[@from='#foo%doesnotexist@{biboumi_host}/{nick_one}']/muc:x",
                              "/presence/error[@type='cancel']/stanza:item-not-found",
                              "/presence/error[@type='cancel']/stanza:text[re:test(text(), '(Domain name not found|Name or service not known)')]")),
                 ]),
        Scenario("simple_channel_join",
                 [
                     handshake_sequence(),
                     partial(send_stanza,
                             "<presence from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}/{nick_one}' />"),
                     connection_sequence("irc.localhost", '{jid_one}/{resource_one}'),
                     partial(expect_stanza,
                             "/message/body[text()='Mode #foo [+nt] by {irc_host_one}']"),
                     partial(expect_stanza,
                             ("/presence[@to='{jid_one}/{resource_one}'][@from='#foo%{irc_server_one}/{nick_one}']/muc_user:x/muc_user:item[@affiliation='admin'][@role='moderator']",
                             "/presence/muc_user:x/muc_user:status[@code='110']")
                             ),
                     partial(expect_stanza, "/message[@from='#foo%{irc_server_one}'][@type='groupchat']/subject[not(text())]"),
                 ]),
        Scenario("raw_names_command",
                 [
                     handshake_sequence(),
                     partial(send_stanza,
                             "<presence from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}/{nick_one}' />"),
                     connection_sequence("irc.localhost", '{jid_one}/{resource_one}'),
                     partial(expect_stanza,
                             "/message/body"),
                     partial(expect_stanza,
                             "/presence/muc_user:x/muc_user:status[@code='110']"
                             ),
                     partial(expect_stanza, "/message/subject[not(text())]"),
                     partial(send_stanza,
                             "<message type='chat' from='{jid_one}/{resource_one}' to='{irc_server_one}'><body>NAMES</body></message>"),
                     partial(expect_stanza, "/message/body[text()='irc.localhost: = #foo @{nick_one} ']"),
                     partial(expect_stanza, "/message/body[text()='irc.localhost: * End of /NAMES list. ']"),
                 ]),
        Scenario("quit",
                 [
                     handshake_sequence(),
                     partial(send_stanza,
                             "<presence from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}/{nick_one}' />"),
                     connection_sequence("irc.localhost", '{jid_one}/{resource_one}'),
                     partial(expect_stanza,
                             "/message/body[text()='Mode #foo [+nt] by {irc_host_one}']"),
                     partial(expect_stanza,
                             ("/presence[@to='{jid_one}/{resource_one}'][@from='#foo%{irc_server_one}/{nick_one}']/muc_user:x/muc_user:item[@affiliation='admin'][@role='moderator']",
                             "/presence/muc_user:x/muc_user:status[@code='110']")
                             ),
                     partial(expect_stanza, "/message[@from='#foo%{irc_server_one}'][@type='groupchat']/subject[not(text())]"),

                     # Send a raw QUIT message
                     partial(send_stanza, "<message from='{jid_one}/{resource_one}' to='{irc_server_one}' type='chat'><body>QUIT bye bye</body></message>"),
                     partial(expect_stanza, ("/presence[@from='#foo%{irc_server_one}/{nick_one}'][@type='unavailable']/muc_user:x/muc_user:status[@code='110']",
                                             "/presence[@from='#foo%{irc_server_one}/{nick_one}'][@type='unavailable']/muc_user:x/muc_user:status[@code='110']",)),
                 ]),
        Scenario("multiple_channels_join",
                 [
                     handshake_sequence(),
                     partial(send_stanza,
                             "<presence from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}/{nick_one}' />"),
                     partial(send_stanza,
                             "<presence from='{jid_one}/{resource_one}' to='#bar%{irc_server_one}/{nick_one}' />"),
                     partial(send_stanza,
                             "<presence from='{jid_one}/{resource_one}' to='#baz%{irc_server_one}/{nick_one}'>  <x xmlns='http://jabber.org/protocol/muc'><password>SECRET</password></x></presence>"),

                     connection_sequence("irc.localhost", '{jid_one}/{resource_one}'),

                     partial(expect_stanza,
                             "/message/body[text()='Mode #foo [+nt] by {irc_host_one}']"),
                     partial(expect_stanza,
                             ("/presence[@to='{jid_one}/{resource_one}'][@from='#foo%{irc_server_one}/{nick_one}']/muc_user:x/muc_user:item[@affiliation='admin'][@role='moderator']",
                              "/presence/muc_user:x/muc_user:status[@code='110']")
                             ),
                     partial(expect_stanza, "/message[@from='#foo%{irc_server_one}'][@type='groupchat']/subject[not(text())]"),

                     partial(expect_stanza,
                             "/message/body[text()='Mode #bar [+nt] by {irc_host_one}']"),
                     partial(expect_stanza,
                             ("/presence[@to='{jid_one}/{resource_one}'][@from='#bar%{irc_server_one}/{nick_one}']/muc_user:x/muc_user:item[@affiliation='admin'][@role='moderator']",
                              "/presence/muc_user:x/muc_user:status[@code='110']")
                             ),
                     partial(expect_stanza, "/message[@from='#bar%{irc_server_one}'][@type='groupchat']/subject[not(text())]"),

                     partial(expect_stanza,
                             "/message/body[text()='Mode #baz [+nt] by {irc_host_one}']"),
                     partial(expect_stanza,
                             ("/presence[@to='{jid_one}/{resource_one}'][@from='#baz%{irc_server_one}/{nick_one}']/muc_user:x/muc_user:item[@affiliation='admin'][@role='moderator']",
                              "/presence/muc_user:x/muc_user:status[@code='110']")
                             ),
                     partial(expect_stanza, "/message[@from='#baz%{irc_server_one}'][@type='groupchat']/subject[not(text())]"),
                 ]),
        Scenario("not_connected_error",
                 [
                     handshake_sequence(),
                     partial(send_stanza,
                             "<presence type='unavailable' from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}/{nick_one}' />"),
                     partial(send_stanza,
                             "<presence from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}/{nick_one}' />"),
                     connection_sequence("irc.localhost", '{jid_one}/{resource_one}'),
                     partial(expect_stanza,
                             "/message/body[text()='Mode #foo [+nt] by {irc_host_one}']"),
                     partial(expect_stanza,
                             ("/presence[@to='{jid_one}/{resource_one}'][@from='#foo%{irc_server_one}/{nick_one}']/muc_user:x/muc_user:item[@affiliation='admin'][@role='moderator']",
                              "/presence/muc_user:x/muc_user:status[@code='110']")
                             ),
                     partial(expect_stanza, "/message[@from='#foo%{irc_server_one}'][@type='groupchat']/subject[not(text())]"),
                 ]),
        Scenario("channel_join_with_two_users",
                 [
                     handshake_sequence(),
                     # First user joins
                     partial(send_stanza,
                             "<presence from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}/{nick_one}' />"),
                     connection_sequence("irc.localhost", '{jid_one}/{resource_one}'),
                     partial(expect_stanza,
                             "/message/body[text()='Mode #foo [+nt] by {irc_host_one}']"),
                     partial(expect_stanza,
                             ("/presence[@to='{jid_one}/{resource_one}'][@from='#foo%{irc_server_one}/{nick_one}']/muc_user:x/muc_user:item[@affiliation='admin'][@jid='~nick@localhost'][@role='moderator']",
                             "/presence/muc_user:x/muc_user:status[@code='110']")
                             ),
                     partial(expect_stanza, "/message[@from='#foo%{irc_server_one}'][@type='groupchat']/subject[not(text())]"),

                     # Second user joins
                     partial(send_stanza,
                             "<presence from='{jid_two}/{resource_one}' to='#foo%{irc_server_one}/{nick_two}' />"),
                     connection_sequence("irc.localhost", '{jid_two}/{resource_one}'),
                     partial(expect_unordered, [
                         ("/presence[@to='{jid_one}/{resource_one}'][@from='#foo%{irc_server_one}/{nick_two}']/muc_user:x/muc_user:item[@affiliation='none'][@role='participant'][@jid='~bobby@localhost']",),
                         ("/presence[@to='{jid_two}/{resource_one}'][@from='#foo%{irc_server_one}/{nick_one}']/muc_user:x/muc_user:item[@affiliation='admin'][@role='moderator']",),
                         ("/presence[@to='{jid_two}/{resource_one}'][@from='#foo%{irc_server_one}/{nick_two}']/muc_user:x/muc_user:item[@affiliation='none'][@jid='~bobby@localhost'][@role='participant']",
                         "/presence/muc_user:x/muc_user:status[@code='110']",),
                         ("/message[@from='#foo%{irc_server_one}'][@type='groupchat']/subject[not(text())]",),
                         ]),
                 ]),
        Scenario("channel_force_join",
                 [
                     handshake_sequence(),
                     # First user joins
                     partial(send_stanza,
                             "<presence from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}/{nick_one}'><x xmlns='http://jabber.org/protocol/muc'/></presence>"),
                     connection_sequence("irc.localhost", '{jid_one}/{resource_one}'),
                     partial(expect_stanza,
                             "/message/body[text()='Mode #foo [+nt] by {irc_host_one}']"),
                     partial(expect_stanza,
                             ("/presence[@to='{jid_one}/{resource_one}'][@from='#foo%{irc_server_one}/{nick_one}']/muc_user:x/muc_user:item[@affiliation='admin'][@jid='~nick@localhost'][@role='moderator']",
                             "/presence/muc_user:x/muc_user:status[@code='110']")
                             ),
                     partial(expect_stanza, "/message[@from='#foo%{irc_server_one}'][@type='groupchat']/subject[not(text())]"),

                     # Second user joins
                     partial(send_stanza,
                             "<presence from='{jid_two}/{resource_one}' to='#foo%{irc_server_one}/{nick_two}'><x xmlns='http://jabber.org/protocol/muc'/></presence>"),
                     connection_sequence("irc.localhost", '{jid_two}/{resource_one}'),
                     partial(expect_unordered, [
                         ("/presence[@to='{jid_one}/{resource_one}'][@from='#foo%{irc_server_one}/{nick_two}']/muc_user:x/muc_user:item[@affiliation='none'][@role='participant'][@jid='~bobby@localhost']",),
                         ("/presence[@to='{jid_two}/{resource_one}'][@from='#foo%{irc_server_one}/{nick_one}']/muc_user:x/muc_user:item[@affiliation='admin'][@role='moderator']",),
                         ("/presence[@to='{jid_two}/{resource_one}'][@from='#foo%{irc_server_one}/{nick_two}']/muc_user:x/muc_user:item[@affiliation='none'][@jid='~bobby@localhost'][@role='participant']",
                         "/presence/muc_user:x/muc_user:status[@code='110']",),
                         ("/message[@from='#foo%{irc_server_one}'][@type='groupchat']/subject[not(text())]",),
                         ]),

                     # Here we simulate a desynchronization of a client: The client thinks it’s
                     # disconnected from the room, but biboumi still thinks it’s in the room. The
                     # client thus sends a join presence, and biboumi should send everything
                     # (user list, history, etc) in response.
                     partial(send_stanza,
                             "<presence from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}/{nick_three}'><x xmlns='http://jabber.org/protocol/muc'/></presence>"),

                     partial(expect_unordered, [
                         ("/presence[@to='{jid_one}/{resource_one}'][@from='#foo%{irc_server_one}/{nick_two}']/muc_user:x/muc_user:item[@affiliation='none'][@role='participant'][@jid='~bobby@localhost']",),
                         ("/presence[@to='{jid_one}/{resource_one}'][@from='#foo%{irc_server_one}/{nick_one}']/muc_user:x/muc_user:item[@affiliation='admin'][@role='moderator']",
                          "/presence/muc_user:x/muc_user:status[@code='110']",),
                         ("/message[@from='#foo%{irc_server_one}'][@type='groupchat']/subject[not(text())]",),
                         ]),
                     # And also, that was not the same nickname
                     partial(expect_unordered, [
                         ("/presence[@from='#foo%{irc_server_one}/{nick_one}'][@to='{jid_two}/{resource_one}'][@type='unavailable']/muc_user:x/muc_user:item[@nick='Bernard']",
                          "/presence/muc_user:x/muc_user:status[@code='303']"),
                         ("/presence[@from='#foo%{irc_server_one}/{nick_three}'][@to='{jid_two}/{resource_one}']",),
                         ("/presence[@from='#foo%{irc_server_one}/{nick_one}'][@to='{jid_one}/{resource_one}'][@type='unavailable']/muc_user:x/muc_user:item[@nick='Bernard']",
                          "/presence/muc_user:x/muc_user:status[@code='303']",
                          "/presence/muc_user:x/muc_user:status[@code='110']"),
                         ("/presence[@from='#foo%{irc_server_one}/{nick_three}'][@to='{jid_one}/{resource_one}']",
                          "/presence/muc_user:x/muc_user:status[@code='110']"),
                     ]),
                 ]),
        Scenario("channel_join_with_password",
                 [
                     handshake_sequence(),
                     # First user joins
                     partial(send_stanza,
                             "<presence from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}/{nick_one}' />"),
                     connection_sequence("irc.localhost", '{jid_one}/{resource_one}'),
                     partial(expect_stanza,
                             "/message/body[text()='Mode #foo [+nt] by {irc_host_one}']"),
                     partial(expect_stanza,
                             ("/presence[@to='{jid_one}/{resource_one}'][@from='#foo%{irc_server_one}/{nick_one}']/muc_user:x/muc_user:item[@affiliation='admin'][@jid='~nick@localhost'][@role='moderator']",
                              "/presence/muc_user:x/muc_user:status[@code='110']")
                             ),
                     partial(expect_stanza, "/message[@from='#foo%{irc_server_one}'][@type='groupchat']/subject[not(text())]"),

                     # Set a password in the room, by using /mode +k
                     partial(send_stanza, "<message from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}' type='groupchat'><body>/mode +k SECRET</body></message>"),
                     partial(expect_stanza, "/message[@from='#foo%{irc_server_one}'][@to='{jid_one}/{resource_one}'][@type='groupchat']/body[text()='Mode #foo [+k SECRET] by {nick_one}']"),

                     # Second user tries to join, without a password
                     partial(send_stanza,
                             "<presence from='{jid_two}/{resource_one}' to='#foo%{irc_server_one}/{nick_two}'/>"),
                     connection_sequence("irc.localhost", '{jid_two}/{resource_one}'),

                     partial(expect_stanza, "/message/body[text()='{irc_host_one}: #foo: Cannot join channel (+k) - bad key']"),
                     partial(expect_stanza,
                             "/presence[@type='error'][@from='#foo%{irc_server_one}/{nick_two}']/error[@type='auth']/stanza:not-authorized",
                     ),

                     # Second user joins, with a password
                     partial(send_stanza,
                             "<presence from='{jid_two}/{resource_one}' to='#foo%{irc_server_one}/{nick_two}'>  <x xmlns='http://jabber.org/protocol/muc'><password>SECRET</password></x></presence>"),
                     # connection_sequence("irc.localhost", '{jid_two}/{resource_one}'),
                     partial(expect_unordered, [
                         ("/presence[@to='{jid_one}/{resource_one}'][@from='#foo%{irc_server_one}/{nick_two}']/muc_user:x/muc_user:item[@affiliation='none'][@role='participant'][@jid='~bobby@localhost']",),
                         ("/presence[@to='{jid_two}/{resource_one}'][@from='#foo%{irc_server_one}/{nick_one}']/muc_user:x/muc_user:item[@affiliation='admin'][@role='moderator']",),
                         ("/presence[@to='{jid_two}/{resource_one}'][@from='#foo%{irc_server_one}/{nick_two}']/muc_user:x/muc_user:item[@affiliation='none'][@jid='~bobby@localhost'][@role='participant']",
                          "/presence/muc_user:x/muc_user:status[@code='110']",),
                         ("/message[@from='#foo%{irc_server_one}'][@type='groupchat']/subject[not(text())]",),
                     ]),

                 ]),
        Scenario("channel_custom_topic",
                 [
                     handshake_sequence(),
                     # First user joins
                     partial(send_stanza,
                             "<presence from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}/{nick_one}' />"),
                     connection_sequence("irc.localhost", '{jid_one}/{resource_one}'),
                     partial(expect_stanza,
                             "/message/body[text()='Mode #foo [+nt] by {irc_host_one}']"),
                     partial(expect_stanza,
                             ("/presence[@to='{jid_one}/{resource_one}'][@from='#foo%{irc_server_one}/{nick_one}']/muc_user:x/muc_user:item[@affiliation='admin'][@jid='~nick@localhost'][@role='moderator']",
                             "/presence/muc_user:x/muc_user:status[@code='110']")
                             ),
                     partial(expect_stanza, "/message[@from='#foo%{irc_server_one}'][@type='groupchat']/subject[not(text())]"),

                     # First user sets the topic
                     partial(send_stanza,
                             "<message from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}' type='groupchat'><subject>TOPIC TEST</subject></message>"),
                     partial(expect_stanza, "/message[@from='#foo%{irc_server_one}/{nick_one}'][@type='groupchat'][@to='{jid_one}/{resource_one}']/subject[text()='TOPIC TEST']"),

                     # Second user joins
                     partial(send_stanza,
                             "<presence from='{jid_two}/{resource_one}' to='#foo%{irc_server_one}/{nick_two}' />"),
                     connection_sequence("irc.localhost", '{jid_two}/{resource_one}'),
                     # Our presence, sent to the other user
                     partial(expect_stanza,
                             ("/presence[@to='{jid_one}/{resource_one}'][@from='#foo%{irc_server_one}/{nick_two}']/muc_user:x/muc_user:item[@affiliation='none'][@jid='~bobby@localhost'][@role='participant']",)),
                     # The other user presence
                     partial(expect_stanza,
                             "/presence[@to='{jid_two}/{resource_one}'][@from='#foo%{irc_server_one}/{nick_one}']/muc_user:x/muc_user:item[@affiliation='admin'][@role='moderator']"),
                     # Our own presence
                     partial(expect_stanza,
                             ("/presence[@to='{jid_two}/{resource_one}'][@from='#foo%{irc_server_one}/{nick_two}']/muc_user:x/muc_user:item[@affiliation='none'][@jid='~bobby@localhost'][@role='participant']",
                              "/presence/muc_user:x/muc_user:status[@code='110']")
                             ),
                     partial(expect_stanza, "/message[@from='#foo%{irc_server_one}/{nick_one}'][@type='groupchat']/subject[text()='TOPIC TEST']"),
                 ]),
        Scenario("multiline_topic",
                 [
                     handshake_sequence(),
                     # User joins
                     partial(send_stanza,
                             "<presence from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}/{nick_one}' />"),
                     connection_sequence("irc.localhost", '{jid_one}/{resource_one}'),
                     partial(expect_stanza,
                             "/message/body"),
                     partial(expect_stanza, "/presence"),
                     partial(expect_stanza, "/message[@from='#foo%{irc_server_one}'][@type='groupchat']/subject[not(text())]"),

                     # User tries to set a multiline topic
                     partial(send_stanza,
                             "<message from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}' type='groupchat'><subject>FIRST LINE\nSECOND LINE.</subject></message>"),
                     partial(expect_stanza, "/message[@from='#foo%{irc_server_one}/{nick_one}'][@type='groupchat'][@to='{jid_one}/{resource_one}']/subject[text()='FIRST LINE SECOND LINE.']"),
                 ]),
        Scenario("channel_basic_join_on_fixed_irc_server",
                 [
                     handshake_sequence(),
                     partial(send_stanza,
                     "<presence from='{jid_one}/{resource_one}' to='#zgeg@{biboumi_host}/{nick_one}' />"),
                     connection_sequence("irc.localhost", '{jid_one}/{resource_one}', fixed_irc_server=True),
                     partial(expect_stanza,
                     "/message/body[text()='Mode #zgeg [+nt] by {irc_host_one}']"),
                     partial(expect_stanza,
                     ("/presence[@to='{jid_one}/{resource_one}'][@from='#zgeg@{biboumi_host}/{nick_one}']/muc_user:x/muc_user:item[@affiliation='admin'][@role='moderator']",
                     "/presence/muc_user:x/muc_user:status[@code='110']")
                     ),
                     partial(expect_stanza, "/message[@from='#zgeg@{biboumi_host}'][@type='groupchat']/subject[not(text())]"),
                 ], conf='fixed_server'
                 ),
        Scenario("list_adhoc",
                 [
                     handshake_sequence(),
                     partial(send_stanza, "<iq type='get' id='idwhatever' from='{jid_one}/{resource_one}' to='{biboumi_host}'><query xmlns='http://jabber.org/protocol/disco#items' node='http://jabber.org/protocol/commands' /></iq>"),
                     partial(expect_stanza, ("/iq[@type='result']/disco_items:query[@node='http://jabber.org/protocol/commands']",
                                             "/iq/disco_items:query/disco_items:item[@node='configure']",
                                             "/iq/disco_items:query/disco_items:item[4]",
                                             "!/iq/disco_items:query/disco_items:item[5]")),
                 ]),
        Scenario("list_admin_adhoc",
                 [
                     handshake_sequence(),
                     partial(send_stanza, "<iq type='get' id='idwhatever' from='{jid_admin}/{resource_one}' to='{biboumi_host}'><query xmlns='http://jabber.org/protocol/disco#items' node='http://jabber.org/protocol/commands' /></iq>"),
                     partial(expect_stanza, ("/iq[@type='result']/disco_items:query[@node='http://jabber.org/protocol/commands']",
                                             "/iq/disco_items:query/disco_items:item[6]",
                                             "!/iq/disco_items:query/disco_items:item[7]")),
                 ]),
        Scenario("list_adhoc_fixed_server",
                 [
                     handshake_sequence(),
                     partial(send_stanza, "<iq type='get' id='idwhatever' from='{jid_one}/{resource_one}' to='{biboumi_host}'><query xmlns='http://jabber.org/protocol/disco#items' node='http://jabber.org/protocol/commands' /></iq>"),
                     partial(expect_stanza, ("/iq[@type='result']/disco_items:query[@node='http://jabber.org/protocol/commands']",
                                             "/iq/disco_items:query/disco_items:item[@node='global-configure']",
                                             "/iq/disco_items:query/disco_items:item[@node='server-configure']",
                                             "/iq/disco_items:query/disco_items:item[6]",
                                             "!/iq/disco_items:query/disco_items:item[7]")),
                 ], conf='fixed_server'),
        Scenario("list_admin_adhoc_fixed_server",
                 [
                     handshake_sequence(),
                     partial(send_stanza, "<iq type='get' id='idwhatever' from='{jid_admin}/{resource_one}' to='{biboumi_host}'><query xmlns='http://jabber.org/protocol/disco#items' node='http://jabber.org/protocol/commands' /></iq>"),
                     partial(expect_stanza, ("/iq[@type='result']/disco_items:query[@node='http://jabber.org/protocol/commands']",
                                             "/iq/disco_items:query/disco_items:item[8]",
                                             "!/iq/disco_items:query/disco_items:item[9]")),
                 ], conf='fixed_server'),
        Scenario("list_adhoc_irc",
                 [
                     handshake_sequence(),
                     partial(send_stanza, "<iq type='get' id='idwhatever' from='{jid_one}/{resource_one}' to='{irc_host_one}@{biboumi_host}'><query xmlns='http://jabber.org/protocol/disco#items' node='http://jabber.org/protocol/commands' /></iq>"),
                     partial(expect_stanza, ("/iq[@type='result']/disco_items:query[@node='http://jabber.org/protocol/commands']",
                                             "/iq/disco_items:query/disco_items:item[2]")),
                 ]),
        Scenario("list_muc_user_adhoc",
                 [
                     handshake_sequence(),
                     partial(send_stanza, "<iq type='get' id='idwhatever' from='{jid_admin}/{resource_one}' to='#foo%{irc_server_one}/{nick_one}'><query xmlns='http://jabber.org/protocol/disco#items' node='http://jabber.org/protocol/commands' /></iq>"),
                     partial(expect_stanza, "/iq[@type='error']/error[@type='cancel']/stanza:feature-not-implemented"),
                 ]
                 ),
        Scenario("execute_hello_adhoc_command",
                 [
                     handshake_sequence(),
                     partial(send_stanza, "<iq type='set' id='hello-command1' from='{jid_one}/{resource_one}' to='{biboumi_host}'><command xmlns='http://jabber.org/protocol/commands' node='hello' action='execute' /></iq>"),
                     partial(expect_stanza, ("/iq[@type='result']/commands:command[@node='hello'][@sessionid][@status='executing']",
                                             "/iq/commands:command/dataform:x[@type='form']/dataform:title[text()='Configure your name.']",
                                             "/iq/commands:command/dataform:x[@type='form']/dataform:instructions[text()='Please provide your name.']",
                                             "/iq/commands:command/dataform:x[@type='form']/dataform:field[@type='text-single']/dataform:required",
                                             "/iq/commands:command/commands:actions/commands:next",
                                             ),
                                             after = partial(save_value, "sessionid", partial(extract_attribute, "/iq[@type='result']/commands:command[@node='hello']", "sessionid"))
                             ),
                     partial(send_stanza, "<iq type='set' id='hello-command2' from='{jid_one}/{resource_one}' to='{biboumi_host}'><command xmlns='http://jabber.org/protocol/commands' node='hello' sessionid='{sessionid}' action='next'><x xmlns='jabber:x:data' type='submit'><field var='name'><value>COUCOU</value></field></x></command></iq>"),
                     partial(expect_stanza, "/iq[@type='result']/commands:command[@node='hello'][@status='completed']/commands:note[@type='info'][text()='Hello COUCOU!']")
                 ]),
        Scenario("execute_incomplete_hello_adhoc_command",
                 [
                     handshake_sequence(),
                     partial(send_stanza, "<iq type='set' id='hello-command1' from='{jid_one}/{resource_one}' to='{biboumi_host}'><command xmlns='http://jabber.org/protocol/commands' node='hello' action='execute' /></iq>"),
                     partial(expect_stanza, ("/iq[@type='result']/commands:command[@node='hello'][@sessionid][@status='executing']",
                                             "/iq/commands:command/commands:actions/commands:next",
                                             ),
                             after = partial(save_value, "sessionid", partial(extract_attribute, "/iq[@type='result']/commands:command[@node='hello']", "sessionid"))
                             ),
                     partial(send_stanza, "<iq type='set' id='hello-command2' from='{jid_one}/{resource_one}' to='{biboumi_host}'><command xmlns='http://jabber.org/protocol/commands' node='hello' sessionid='{sessionid}' action='next'><x xmlns='jabber:x:data' type='submit'></x></command></iq>"),
                     partial(expect_stanza, "/iq[@type='error']")
                 ]),
        Scenario("execute_ping_adhoc_command",
                 [
                     handshake_sequence(),
                     partial(send_stanza, "<iq type='set' id='ping-command1' from='{jid_one}/{resource_one}' to='{biboumi_host}'><command xmlns='http://jabber.org/protocol/commands' node='ping' action='execute' /></iq>"),
                     partial(expect_stanza, "/iq[@type='result']/commands:command[@node='ping'][@status='completed']/commands:note[@type='info'][text()='Pong']")
                 ]),
        Scenario("execute_reload_adhoc_command",
                 [
                     handshake_sequence(),
                     partial(send_stanza, "<iq type='set' id='ping-command1' from='{jid_admin}/{resource_one}' to='{biboumi_host}'><command xmlns='http://jabber.org/protocol/commands' node='reload' action='execute' /></iq>"),
                     partial(expect_stanza, "/iq[@type='result']/commands:command[@node='reload'][@status='completed']/commands:note[@type='info'][text()='Configuration reloaded.']")
                 ]),
        Scenario("execute_forbidden_adhoc_command",
                 [
                     handshake_sequence(),
                     partial(send_stanza, "<iq type='set' id='command1' from='{jid_one}/{resource_one}' to='{biboumi_host}'><command xmlns='http://jabber.org/protocol/commands' node='disconnect-user' action='execute' /></iq>"),
                     partial(expect_stanza, ("/iq[@type='error'][@id='command1']/commands:command[@node='disconnect-user']",
                                             "/iq/commands:command/commands:error[@type='cancel']/stanza:forbidden")),
                 ]),
        Scenario("execute_disconnect_user_adhoc_command",
                 [
                     handshake_sequence(),

                     partial(send_stanza, "<presence from='{jid_admin}/{resource_one}' to='#foo%{irc_server_one}/{nick_one}' />"),
                     connection_sequence("irc.localhost", '{jid_admin}/{resource_one}'),
                     partial(expect_stanza, "/message/body[text()='Mode #foo [+nt] by {irc_host_one}']"),
                     partial(expect_stanza, "/presence"),
                     partial(expect_stanza, "/message"),

                     partial(send_stanza, "<iq type='set' id='command1' from='{jid_admin}/{resource_one}' to='{biboumi_host}'><command xmlns='http://jabber.org/protocol/commands' node='disconnect-user' action='execute' /></iq>"),
                     partial(expect_stanza, ("/iq[@type='result']/commands:command[@node='disconnect-user'][@sessionid][@status='executing']",
                                             "/iq/commands:command/commands:actions/commands:next",
                                             ),
                                             after = partial(save_value, "sessionid", partial(extract_attribute, "/iq/commands:command[@node='disconnect-user']", "sessionid"))
                             ),
                     partial(send_stanza, "<iq type='set' id='command2' from='{jid_admin}/{resource_one}' to='{biboumi_host}'><command xmlns='http://jabber.org/protocol/commands' node='disconnect-user' sessionid='{sessionid}' action='next'><x xmlns='jabber:x:data' type='submit'><field var='jids'><value>{jid_admin}</value></field><field var='quit-message'><value>Disconnected by e2e</value></field></x></command></iq>"),
                     partial(expect_stanza, "/iq[@type='result']/commands:command[@node='disconnect-user'][@status='completed']/commands:note[@type='info'][text()='1 user has been disconnected.']"),
                     # Note, charybdis ignores our QUIT message, so we can't test it
                     partial(expect_stanza, "/presence[@type='unavailable'][@to='{jid_admin}/{resource_one}'][@from='#foo%{irc_server_one}/{nick_one}']"),
                 ]),
        Scenario("execute_admin_disconnect_from_server_adhoc_command",
                 [
                     handshake_sequence(),

                     # Admin connects to first server
                     partial(send_stanza, "<presence from='{jid_admin}/{resource_one}' to='#bar%{irc_server_one}/{nick_one}' />"),
                     connection_sequence("irc.localhost", '{jid_admin}/{resource_one}'),
                     partial(expect_stanza, "/message/body[text()='Mode #bar [+nt] by {irc_host_one}']"),
                     partial(expect_stanza, "/presence"),
                     partial(expect_stanza, "/message"),

                     # Non-Admin connects to first server
                     partial(send_stanza, "<presence from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}/{nick_two}' />"),
                     connection_sequence("irc.localhost", '{jid_one}/{resource_one}'),
                     partial(expect_stanza, "/message/body[text()='Mode #foo [+nt] by {irc_host_one}']"),
                     partial(expect_stanza, "/presence"),
                     partial(expect_stanza, "/message"),

                     # Non-admin connects to second server
                     partial(send_stanza, "<presence from='{jid_one}/{resource_one}' to='#bon%{irc_server_two}/{nick_three}' />"),
                     connection_sequence("localhost", '{jid_one}/{resource_one}'),
                     partial(expect_stanza, "/message/body[text()='Mode #bon [+nt] by {irc_host_one}']"),
                     partial(expect_stanza, "/presence"),
                     partial(expect_stanza, "/message"),

                     # Execute as admin
                     partial(send_stanza, "<iq type='set' id='command1' from='{jid_admin}/{resource_one}' to='{biboumi_host}'><command xmlns='http://jabber.org/protocol/commands' node='disconnect-from-irc-server' action='execute' /></iq>"),
                     partial(expect_stanza, ("/iq[@type='result']/commands:command[@node='disconnect-from-irc-server'][@sessionid][@status='executing']",
                                             "/iq/commands:command/dataform:x[@type='form']/dataform:field[@var='jid'][@type='list-single']/dataform:option[@label='{jid_one}']/dataform:value[text()='{jid_one}']",
                                             "/iq/commands:command/dataform:x[@type='form']/dataform:field[@var='jid'][@type='list-single']/dataform:option[@label='{jid_admin}']/dataform:value[text()='{jid_admin}']",
                                             "/iq/commands:command/commands:actions/commands:next",
                                             ),
                             after = partial(save_value, "sessionid", partial(extract_attribute, "/iq/commands:command[@node='disconnect-from-irc-server']", "sessionid"))
                             ),
                     partial(send_stanza, "<iq type='set' id='command2' from='{jid_admin}/{resource_one}' to='{biboumi_host}'><command xmlns='http://jabber.org/protocol/commands' node='disconnect-from-irc-server' sessionid='{sessionid}' action='next'><x xmlns='jabber:x:data' type='submit'><field var='jid'><value>{jid_one}</value></field><field var='quit-message'><value>e2e test one</value></field></x></command></iq>"),

                     partial(expect_stanza, ("/iq[@type='result']/commands:command[@node='disconnect-from-irc-server'][@sessionid][@status='executing']",
                                             "/iq/commands:command/dataform:x[@type='form']/dataform:field[@var='quit-message'][@type='text-single']",
                                             "/iq/commands:command/dataform:x[@type='form']/dataform:field[@var='irc-servers'][@type='list-multi']",
                                             "/iq/commands:command/dataform:x[@type='form']/dataform:field[@var='irc-servers']/dataform:option[@label='localhost']/dataform:value[text()='localhost']",
                                             "/iq/commands:command/dataform:x[@type='form']/dataform:field[@var='irc-servers']/dataform:option[@label='irc.localhost']/dataform:value[text()='irc.localhost']",
                                             "/iq/commands:command/commands:actions/commands:next",
                                             ),
                             after = partial(save_value, "sessionid", partial(extract_attribute, "/iq/commands:command[@node='disconnect-from-irc-server']", "sessionid"))
                             ),
                     partial(send_stanza, "<iq type='set' id='command2' from='{jid_admin}/{resource_one}' to='{biboumi_host}'><command xmlns='http://jabber.org/protocol/commands' node='disconnect-from-irc-server' sessionid='{sessionid}' action='next'><x xmlns='jabber:x:data' type='submit'><field var='irc-servers'><value>localhost</value></field><field var='quit-message'><value>Disconnected by e2e</value></field></x></command></iq>"),
                     partial(expect_unordered, [("/presence[@type='unavailable'][@to='{jid_one}/{resource_one}'][@from='#bon%{irc_server_two}/{nick_three}']",),
                                                ("/iq[@type='result']/commands:command[@node='disconnect-from-irc-server'][@status='completed']/commands:note[@type='info'][text()='{jid_one} was disconnected from 1 IRC server.']",),
                                                ]),


                     # Execute as non-admin (this skips the first step)
                     partial(send_stanza, "<iq type='set' id='command1' from='{jid_one}/{resource_one}' to='{biboumi_host}'><command xmlns='http://jabber.org/protocol/commands' node='disconnect-from-irc-server' action='execute' /></iq>"),

                     partial(expect_stanza, ("/iq[@type='result']/commands:command[@node='disconnect-from-irc-server'][@sessionid][@status='executing']",
                                             "/iq/commands:command/dataform:x[@type='form']/dataform:field[@var='quit-message'][@type='text-single']",
                                             "/iq/commands:command/dataform:x[@type='form']/dataform:field[@var='irc-servers'][@type='list-multi']",
                                             "/iq/commands:command/dataform:x[@type='form']/dataform:field[@var='irc-servers']/dataform:option[@label='irc.localhost']/dataform:value[text()='irc.localhost']",
                                             "/iq/commands:command/commands:actions/commands:next",
                                             ),
                             after = partial(save_value, "sessionid", partial(extract_attribute, "/iq/commands:command[@node='disconnect-from-irc-server']", "sessionid"))
                             ),
                     partial(send_stanza, "<iq type='set' id='command2' from='{jid_one}/{resource_one}' to='{biboumi_host}'><command xmlns='http://jabber.org/protocol/commands' node='disconnect-from-irc-server' sessionid='{sessionid}' action='next'><x xmlns='jabber:x:data' type='submit'><field var='irc-servers'><value>irc.localhost</value></field><field var='quit-message'><value>Disconnected by e2e</value></field></x></command></iq>"),
                     partial(expect_unordered, [("/presence[@type='unavailable'][@to='{jid_one}/{resource_one}'][@from='#foo%{irc_server_one}/{nick_two}']",),
                                                ("/iq[@type='result']/commands:command[@node='disconnect-from-irc-server'][@status='completed']/commands:note[@type='info'][text()='{jid_one}/{resource_one} was disconnected from 1 IRC server.']",),
                                                ]),
                 ]),
        Scenario("multisessionnick",
                 [
                     handshake_sequence(),
                     partial(send_stanza,
                             "<presence from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}/{nick_one}' />"),
                     connection_sequence("irc.localhost", '{jid_one}/{resource_one}'),
                     partial(expect_stanza,
                             "/message/body[text()='Mode #foo [+nt] by {irc_host_one}']"),
                     partial(expect_stanza,
                             ("/presence[@to='{jid_one}/{resource_one}'][@from='#foo%{irc_server_one}/{nick_one}']/muc_user:x/muc_user:item[@affiliation='admin'][@role='moderator']",
                              "/presence/muc_user:x/muc_user:status[@code='110']")
                             ),
                     partial(expect_stanza, "/message[@from='#foo%{irc_server_one}'][@type='groupchat'][@to='{jid_one}/{resource_one}']/subject[not(text())]"),

                     # The other resources joins the same room, with the same nick
                     partial(send_stanza,
                             "<presence from='{jid_one}/{resource_two}' to='#foo%{irc_server_one}/{nick_one}' />"),
                     # We receive our own join
                     partial(expect_unordered,
                             [("/presence[@to='{jid_one}/{resource_two}'][@from='#foo%{irc_server_one}/{nick_one}']/muc_user:x/muc_user:item[@affiliation='admin'][@role='moderator']",
                              "/presence/muc_user:x/muc_user:status[@code='110']"),
                              ("/message[@from='#foo%{irc_server_one}'][@type='groupchat'][@to='{jid_one}/{resource_two}']/subject[not(text())]",)]
                             ),

                     # A different user joins the same room
                     partial(send_stanza,
                             "<presence from='{jid_two}/{resource_one}' to='#foo%{irc_server_one}/{nick_two}' />"),
                     connection_sequence("irc.localhost", '{jid_two}/{resource_one}'),

                     partial(expect_unordered, [
                                ("/presence[@to='{jid_one}/{resource_one}'][@from='#foo%{irc_server_one}/{nick_two}']",),
                                ("/presence[@to='{jid_one}/{resource_two}'][@from='#foo%{irc_server_one}/{nick_two}']",),
                                ("/presence[@to='{jid_two}/{resource_one}'][@from='#foo%{irc_server_one}/{nick_two}']",
                                 "/presence/muc_user:x/muc_user:status[@code='110']",),
                                ("/presence[@to='{jid_two}/{resource_one}'][@from='#foo%{irc_server_one}/{nick_one}']",),
                                ("/message[@from='#foo%{irc_server_one}'][@type='groupchat']/subject[not(text())]",),
                                ]
                             ),

                     # That second user sends a private message to the first one
                     partial(send_stanza, "<message from='{jid_two}/{resource_one}' to='#foo%{irc_server_one}/{nick_one}' type='chat'><body>RELLO</body></message>"),
                     # Message is received with a server-wide JID, by the two resources behind nick_one
                     partial(expect_stanza, ("/message[@from='{lower_nick_two}%{irc_server_one}'][@to='{jid_one}/{resource_one}'][@type='chat']/body[text()='RELLO']",
                                             "/message/hints:no-copy",
                                             "/message/carbon:private",
                                             "!/message/muc_user:x")),
                     partial(expect_stanza, "/message[@from='{lower_nick_two}%{irc_server_one}'][@to='{jid_one}/{resource_two}'][@type='chat']/body[text()='RELLO']"),


                     # First occupant (with the two resources) changes her/his nick
                     partial(send_stanza, "<presence from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}/{nick_two}' />"),
                     partial(expect_unordered, [
                         ("/message[@to='{jid_one}/{resource_one}'][@type='chat']/body[text()='irc.localhost: Bobby: Nickname is already in use.']",),
                         ("/message[@to='{jid_one}/{resource_two}'][@type='chat']/body[text()='irc.localhost: Bobby: Nickname is already in use.']",),
                         ("/presence[@to='{jid_one}/{resource_one}'][@from='#foo%{irc_server_one}/{nick_two}'][@type='error']",),
                         ("/presence[@to='{jid_one}/{resource_two}'][@from='#foo%{irc_server_one}/{nick_two}'][@type='error']",),
                     ]),

                     # First occupant (with the two resources) changes her/his nick
                     partial(send_stanza, "<presence from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}/{nick_three}' />"),
                     partial(expect_unordered, [
                         ("/presence[@from='#foo%{irc_server_one}/{nick_one}'][@to='{jid_two}/{resource_one}'][@type='unavailable']/muc_user:x/muc_user:item[@nick='Bernard']",
                          "/presence/muc_user:x/muc_user:status[@code='303']"),
                         ("/presence[@from='#foo%{irc_server_one}/{nick_three}'][@to='{jid_two}/{resource_one}']",),
                         ("/presence[@from='#foo%{irc_server_one}/{nick_one}'][@to='{jid_one}/{resource_one}'][@type='unavailable']/muc_user:x/muc_user:item[@nick='Bernard']",
                          "/presence/muc_user:x/muc_user:status[@code='303']",
                          "/presence/muc_user:x/muc_user:status[@code='110']"),
                         ("/presence[@from='#foo%{irc_server_one}/{nick_three}'][@to='{jid_one}/{resource_one}']",
                          "/presence/muc_user:x/muc_user:status[@code='110']"),

                         ("/presence[@from='#foo%{irc_server_one}/{nick_one}'][@to='{jid_one}/{resource_two}'][@type='unavailable']/muc_user:x/muc_user:item[@nick='Bernard']",
                          "/presence/muc_user:x/muc_user:status[@code='303']",
                          "/presence/muc_user:x/muc_user:status[@code='110']"),
                         ("/presence[@from='#foo%{irc_server_one}/{nick_three}'][@to='{jid_one}/{resource_two}']",
                          "/presence/muc_user:x/muc_user:status[@code='110']"),
                     ]),

                     # One resource leaves the server entirely.
                     partial(send_stanza, "<presence type='unavailable' from='{jid_one}/{resource_two}' to='#foo%{irc_server_one}/{nick_one}' />"),
                     # The leave is forwarded only to us
                     partial(expect_stanza,
                             ("/presence[@type='unavailable']/muc_user:x/muc_user:status[@code='110']",
                              "/presence/status[text()='Biboumi note: 1 resources are still in this channel.']",
                              )
                             ),

                     # The second user sends two new private messages to the first user
                     partial(send_stanza, "<message from='{jid_two}/{resource_one}' to='#foo%{irc_server_one}/{nick_three}' type='chat'><body>first</body></message>"),
                     partial(send_stanza, "<message from='{jid_two}/{resource_one}' to='#foo%{irc_server_one}/{nick_three}' type='chat'><body>second</body></message>"),
                     # The first user receives the two messages, on the connected resource, once each

                     partial(expect_unordered, [
                         ("/message[@from='{lower_nick_two}%{irc_server_one}'][@to='{jid_one}/{resource_one}'][@type='chat']/body[text()='first']",),
                         ("/message[@from='{lower_nick_two}%{irc_server_one}'][@to='{jid_one}/{resource_one}'][@type='chat']/body[text()='second']",),
                         ]),
                 ]),
        Scenario("persistent_channel",
                 [
                     # Join the channel with user 1
                     handshake_sequence(),
                     partial(send_stanza,
                             "<presence from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}/{nick_one}' />"),
                     connection_sequence("irc.localhost", '{jid_one}/{resource_one}'),
                     partial(expect_stanza,
                             "/message/body[text()='Mode #foo [+nt] by {irc_host_one}']"),
                     partial(expect_stanza,
                             ("/presence[@to='{jid_one}/{resource_one}'][@from='#foo%{irc_server_one}/{nick_one}']/muc_user:x/muc_user:item[@affiliation='admin'][@role='moderator']",
                              "/presence/muc_user:x/muc_user:status[@code='110']")
                             ),
                     partial(expect_stanza, "/message[@from='#foo%{irc_server_one}'][@type='groupchat'][@to='{jid_one}/{resource_one}']/subject[not(text())]"),

                     # Make it persistent for user 1
                     partial(send_stanza, "<iq from='{jid_one}/{resource_one}' id='conf1' to='#foo%{irc_server_one}' type='get'><query xmlns='http://jabber.org/protocol/muc#owner'/></iq>"),
                     partial(expect_stanza, "/iq[@type='result']/muc_owner:query/dataform:x/dataform:field[@var='persistent'][@type='boolean']/dataform:value[text()='false']"),
                     partial(send_stanza, "<iq from='{jid_one}/{resource_one}' id='conf2' to='#foo%{irc_server_one}' type='set'><query xmlns='http://jabber.org/protocol/muc#owner'><x type='submit' xmlns='jabber:x:data'><field var='persistent' xmlns='jabber:x:data'><value>true</value></field></x></query></iq>"),
                     partial(expect_stanza, "/iq[@type='result']"),

                     # Check that the value is now effectively true
                     partial(send_stanza, "<iq from='{jid_one}/{resource_one}' id='conf1' to='#foo%{irc_server_one}' type='get'><query xmlns='http://jabber.org/protocol/muc#owner'/></iq>"),
                     partial(expect_stanza, "/iq[@type='result']/muc_owner:query/dataform:x/dataform:field[@var='persistent'][@type='boolean']/dataform:value[text()='true']"),

                     # A second user joins the same channel
                     partial(send_stanza,
                             "<presence from='{jid_two}/{resource_one}' to='#foo%{irc_server_one}/{nick_two}' />"),
                     connection_sequence("irc.localhost", '{jid_two}/{resource_one}'),

                     partial(expect_unordered, [
                                ("/presence[@to='{jid_one}/{resource_one}'][@from='#foo%{irc_server_one}/{nick_two}']",),
                                ("/presence[@to='{jid_two}/{resource_one}'][@from='#foo%{irc_server_one}/{nick_two}']",
                                "/presence/muc_user:x/muc_user:status[@code='110']",),
                                ("/presence[@to='{jid_two}/{resource_one}'][@from='#foo%{irc_server_one}/{nick_one}']",),
                                ]
                             ),

                     partial(expect_stanza, "/message[@from='#foo%{irc_server_one}'][@type='groupchat']/subject[not(text())]"),

                     # First user leaves the room (but biboumi will stay in the channel)
                     partial(send_stanza,
                     "<presence from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}/{nick_one}' type='unavailable' />"),
                     # Only user 1 receives the unavailable presence
                     partial(expect_stanza,
                             ("/presence[@from='#foo%{irc_server_one}/{nick_one}'][@to='{jid_one}/{resource_one}'][@type='unavailable']/muc_user:x/muc_user:status[@code='110']",
                              "/presence/muc_user:x/muc_user:item[@affiliation='admin'][@role='moderator']")),

                     # Second user sends a channel message
                     partial(send_stanza, "<message type='groupchat' from='{jid_two}/{resource_one}' to='#foo%{irc_server_one}'><body>coucou</body></message>"),

                     # Message should only be received by user 2, since user 1 has no resource in the room
                     partial(expect_stanza, "/message[@type='groupchat'][@to='{jid_two}/{resource_one}'][@from='#foo%{irc_server_one}/{nick_two}']"),

                     # Second user leaves the channel
                     partial(send_stanza, "<presence type='unavailable' from='{jid_two}/{resource_one}' to='#foo%{irc_server_one}/{nick_two}' />"),
                     partial(expect_stanza, "/presence[@type='unavailable'][@from='#foo%{irc_server_one}/{nick_two}']"),
                 ]),
        Scenario("channel_join_with_different_nick",
                 [
                     handshake_sequence(),
                     partial(send_stanza,
                             "<presence from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}/{nick_one}' />"),
                     connection_sequence("irc.localhost", '{jid_one}/{resource_one}'),
                     partial(expect_stanza,
                             "/message/body[text()='Mode #foo [+nt] by {irc_host_one}']"),
                     partial(expect_stanza,
                             ("/presence[@to='{jid_one}/{resource_one}'][@from='#foo%{irc_server_one}/{nick_one}']/muc_user:x/muc_user:item[@affiliation='admin'][@role='moderator']",
                              "/presence/muc_user:x/muc_user:status[@code='110']")
                             ),
                     partial(expect_stanza, "/message[@from='#foo%{irc_server_one}'][@type='groupchat'][@to='{jid_one}/{resource_one}']/subject[not(text())]"),

                     # The same resource joins a different channel with a different nick
                     partial(send_stanza,
                             "<presence from='{jid_one}/{resource_one}' to='#bar%{irc_server_one}/{nick_two}' />"),

                     # We must receive a join presence in response, without any nick change (nick_two) must be ignored
                     partial(expect_stanza,
                             "/message/body[text()='Mode #bar [+nt] by {irc_host_one}']"),
                     partial(expect_stanza,
                             ("/presence[@to='{jid_one}/{resource_one}'][@from='#bar%{irc_server_one}/{nick_one}']/muc_user:x/muc_user:item[@affiliation='admin'][@role='moderator']",
                              "/presence/muc_user:x/muc_user:status[@code='110']")
                             ),
                     partial(expect_stanza, "/message[@from='#bar%{irc_server_one}'][@type='groupchat'][@to='{jid_one}/{resource_one}']/subject[not(text())]"),
                 ]),
        Scenario("notices",
                 [
                     handshake_sequence(),
                     partial(send_stanza,
                             "<presence from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}/{nick_one}' />"),
                     connection_sequence("irc.localhost", '{jid_one}/{resource_one}'),
                     partial(expect_stanza, "/message"),
                     partial(expect_stanza, "/presence"),
                     partial(expect_stanza, "/message"),

                     partial(send_stanza, "<message from='{jid_one}/{resource_one}' to='{irc_server_one}' type='chat'><body>NOTICE {nick_one} :[#foo] Hello in a notice.</body></message>"),
                     partial(expect_stanza, "/message[@from='#foo%{irc_server_one}/{nick_one}'][@type='groupchat']/body[text()='[notice] [#foo] Hello in a notice.']"),
                 ]),
        Scenario("multiline_message",
                 [
                     handshake_sequence(),
                     # First user joins
                     partial(send_stanza,
                     "<presence from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}/{nick_one}' />"),
                     connection_sequence("irc.localhost", '{jid_one}/{resource_one}'),
                     partial(expect_stanza,
                     "/message/body[text()='Mode #foo [+nt] by {irc_host_one}']"),
                     partial(expect_stanza,
                     ("/presence[@to='{jid_one}/{resource_one}'][@from='#foo%{irc_server_one}/{nick_one}']/muc_user:x/muc_user:item[@affiliation='admin'][@role='moderator']",
                     "/presence/muc_user:x/muc_user:status[@code='110']")
                     ),
                     partial(expect_stanza, "/message[@from='#foo%{irc_server_one}'][@type='groupchat']/subject[not(text())]"),

                     # Send a multi-line channel message
                     partial(send_stanza, "<message id='the-message-id' from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}' type='groupchat'><body>un\ndeux\ntrois</body></message>"),
                     # Receive multiple messages, in order
                     partial(expect_stanza,
                         "/message[@from='#foo%{irc_server_one}/{nick_one}'][@id='the-message-id'][@to='{jid_one}/{resource_one}'][@type='groupchat']/body[text()='un']"),
                     partial(expect_stanza,
                         "/message[@from='#foo%{irc_server_one}/{nick_one}'][@id][@to='{jid_one}/{resource_one}'][@type='groupchat']/body[text()='deux']"),
                     partial(expect_stanza,
                         "/message[@from='#foo%{irc_server_one}/{nick_one}'][@id][@to='{jid_one}/{resource_one}'][@type='groupchat']/body[text()='trois']"),

                     # Send a simple message, with no id
                     partial(send_stanza, "<message from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}' type='groupchat'><body>hello</body></message>"),

                     # Expect a non-empty id as a result (should be a uuid)
                     partial(expect_stanza,
                             ("!/message[@id='']/body[text()='hello']",
                              "/message[@id]/body[text()='hello']")),

                     # even though we reflect the message to XMPP only
                     # when we send it to IRC, there’s still a race
                     # condition if the XMPP client receives the
                     # reflection (and the IRC server didn’t yet receive
                     # it), then the new user joins the room, and then
                     # finally the IRC server sends the message to “all
                     # participants of the channel”, including the new
                     # one, that was not supposed to be there when the
                     # message was sent in the first place by the first
                     # XMPP user. There’s nothing we can do about it until
                     # all servers support the echo-message IRCv3
                     # extension… So, we just sleep a little bit before
                     # joining the room with the new user.
                     partial(sleep_for, 1),

                     # Second user joins
                     partial(send_stanza,
                     "<presence from='{jid_two}/{resource_one}' to='#foo%{irc_server_one}/{nick_two}' />"),
                     connection_sequence("irc.localhost", '{jid_two}/{resource_one}'),
                     # Our presence, sent to the other user
                     partial(expect_unordered, [
                         ("/presence[@to='{jid_one}/{resource_one}'][@from='#foo%{irc_server_one}/{nick_two}']/muc_user:x/muc_user:item[@affiliation='none'][@jid='~bobby@localhost'][@role='participant']",),
                         ("/presence[@to='{jid_two}/{resource_one}'][@from='#foo%{irc_server_one}/{nick_one}']/muc_user:x/muc_user:item[@affiliation='admin'][@role='moderator']",),
                         ("/presence[@to='{jid_two}/{resource_one}'][@from='#foo%{irc_server_one}/{nick_two}']/muc_user:x/muc_user:item[@affiliation='none'][@jid='~bobby@localhost'][@role='participant']",
                          "/presence/muc_user:x/muc_user:status[@code='110']"),
                         ("/message[@from='#foo%{irc_server_one}'][@type='groupchat']/subject[not(text())]",)
                     ]),

                     # Send a multi-line channel message
                     partial(send_stanza, "<message id='the-message-id' from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}' type='groupchat'><body>a\nb\nc</body></message>"),
                     # Receive multiple messages, for each user
                     partial(expect_unordered, [
                         ("/message[@from='#foo%{irc_server_one}/{nick_one}'][@id='the-message-id'][@to='{jid_one}/{resource_one}'][@type='groupchat']/body[text()='a']",),
                         ("/message[@from='#foo%{irc_server_one}/{nick_one}'][@id][@to='{jid_one}/{resource_one}'][@type='groupchat']/body[text()='b']",),
                         ("/message[@from='#foo%{irc_server_one}/{nick_one}'][@id][@to='{jid_one}/{resource_one}'][@type='groupchat']/body[text()='c']",),

                         ("/message[@from='#foo%{irc_server_one}/{nick_one}'][@id][@to='{jid_two}/{resource_one}'][@type='groupchat']/body[text()='a']",),
                         ("/message[@from='#foo%{irc_server_one}/{nick_one}'][@id][@to='{jid_two}/{resource_one}'][@type='groupchat']/body[text()='b']",),
                         ("/message[@from='#foo%{irc_server_one}/{nick_one}'][@id][@to='{jid_two}/{resource_one}'][@type='groupchat']/body[text()='c']",),
                     ])
                 ]),
        Scenario("channel_messages",
                 [
                     handshake_sequence(),
                     # First user joins
                     partial(send_stanza,
                     "<presence from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}/{nick_one}' />"),
                     connection_sequence("irc.localhost", '{jid_one}/{resource_one}'),
                     partial(expect_stanza,
                     "/message/body[text()='Mode #foo [+nt] by {irc_host_one}']"),
                     partial(expect_stanza,
                     ("/presence[@to='{jid_one}/{resource_one}'][@from='#foo%{irc_server_one}/{nick_one}']/muc_user:x/muc_user:item[@affiliation='admin'][@role='moderator']",
                     "/presence/muc_user:x/muc_user:status[@code='110']")
                     ),
                     partial(expect_stanza, "/message[@from='#foo%{irc_server_one}'][@type='groupchat']/subject[not(text())]"),

                     # Second user joins
                     partial(send_stanza,
                     "<presence from='{jid_two}/{resource_one}' to='#foo%{irc_server_one}/{nick_two}' />"),
                     connection_sequence("irc.localhost", '{jid_two}/{resource_one}'),
                     # Our presence, sent to the other user
                     partial(expect_unordered, [
                         ("/presence[@to='{jid_one}/{resource_one}'][@from='#foo%{irc_server_one}/{nick_two}']/muc_user:x/muc_user:item[@affiliation='none'][@jid='~bobby@localhost'][@role='participant']",),
                         ("/presence[@to='{jid_two}/{resource_one}'][@from='#foo%{irc_server_one}/{nick_one}']/muc_user:x/muc_user:item[@affiliation='admin'][@role='moderator']",),
                         ("/presence[@to='{jid_two}/{resource_one}'][@from='#foo%{irc_server_one}/{nick_two}']/muc_user:x/muc_user:item[@affiliation='none'][@jid='~bobby@localhost'][@role='participant']",
                          "/presence/muc_user:x/muc_user:status[@code='110']"),
                         ("/message[@from='#foo%{irc_server_one}'][@type='groupchat']/subject[not(text())]",)
                     ]),

                     # Send a channel message
                     partial(send_stanza, "<message from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}' type='groupchat'><body>coucou</body></message>"),
                     # Receive the message, forwarded to the two users
                     partial(expect_unordered, [
                         ("/message[@from='#foo%{irc_server_one}/{nick_one}'][@to='{jid_one}/{resource_one}'][@type='groupchat']/body[text()='coucou']",
                          "/message/stable_id:stanza-id[@by='#foo%{irc_server_one}'][@id]"),
                         ("/message[@from='#foo%{irc_server_one}/{nick_one}'][@to='{jid_two}/{resource_one}'][@type='groupchat']/body[text()='coucou']",
                          "/message/stable_id:stanza-id[@by='#foo%{irc_server_one}'][@id]")
                         ]),

                     # Send a private message, to a in-room JID
                     partial(send_stanza, "<message from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}/{nick_two}' type='chat'><body>coucou in private</body></message>"),
                     # Message is received with a server-wide JID
                     partial(expect_stanza, "/message[@from='{lower_nick_one}%{irc_server_one}'][@to='{jid_two}/{resource_one}'][@type='chat']/body[text()='coucou in private']"),

                     # Respond to the message, to the server-wide JID
                     partial(send_stanza, "<message from='{jid_two}/{resource_one}' to='{lower_nick_one}%{irc_server_one}' type='chat'><body>yes</body></message>"),
                     # The response is received from the in-room JID
                     partial(expect_stanza, ("/message[@from='#foo%{irc_server_one}/{nick_two}'][@to='{jid_one}/{resource_one}'][@type='chat']/body[text()='yes']",
                                             "/message/muc_user:x")),

                     ## Do the exact same thing, from a different chan,
                     # to check if the response comes from the right JID

                     partial(send_stanza,
                     "<presence from='{jid_one}/{resource_one}' to='#dummy%{irc_server_one}/{nick_one}' />"),
                     partial(expect_stanza, "/message"),
                     partial(expect_stanza,
                     "/presence/muc_user:x/muc_user:status[@code='110']"),
                     partial(expect_stanza, "/message[@from='#dummy%{irc_server_one}'][@type='groupchat']/subject"),


                     # Send a private message, to a in-room JID
                     partial(send_stanza, "<message from='{jid_one}/{resource_one}' to='#dummy%{irc_server_one}/{nick_two}' type='chat'><body>re in private</body></message>"),
                     # Message is received with a server-wide JID
                     partial(expect_stanza, "/message[@from='{lower_nick_one}%{irc_server_one}'][@to='{jid_two}/{resource_one}'][@type='chat']/body[text()='re in private']"),

                     # Respond to the message, to the server-wide JID
                     partial(send_stanza, "<message from='{jid_two}/{resource_one}' to='{lower_nick_one}%{irc_server_one}' type='chat'><body>re</body></message>"),
                     # The response is received from the in-room JID
                     partial(expect_stanza, "/message[@from='#dummy%{irc_server_one}/{nick_two}'][@to='{jid_one}/{resource_one}'][@type='chat']/body[text()='re']"),

                     # Now we leave the room, to check if the subsequent private messages are still received properly
                     partial(send_stanza,
                     "<presence from='{jid_one}/{resource_one}' to='#dummy%{irc_server_one}/{nick_one}' type='unavailable' />"),
                     partial(expect_stanza,
                     "/presence[@type='unavailable']/muc_user:x/muc_user:status[@code='110']"),

                     # The private messages from this nick should now come (again) from the server-wide JID
                     partial(send_stanza, "<message from='{jid_two}/{resource_one}' to='{lower_nick_one}%{irc_server_one}' type='chat'><body>hihihoho</body></message>"),
                     partial(expect_stanza,
                     "/message[@from='{lower_nick_two}%{irc_server_one}'][@to='{jid_one}/{resource_one}']"),
                 ]
                 ),
                Scenario("muc_message_from_unjoined_resource",
                         [
                         handshake_sequence(),
                         partial(send_stanza,
                                 "<presence from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}/{nick_one}' />"),
                         connection_sequence("irc.localhost", '{jid_one}/{resource_one}'),
                         partial(expect_stanza,
                                 "/message/body[text()='Mode #foo [+nt] by {irc_host_one}']"),
                         partial(expect_stanza, "/presence"),
                         partial(expect_stanza, "/message/subject"),

                         # Send a channel message
                        partial(send_stanza, "<message from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}' type='groupchat'><body>coucou</body></message>"),
                        # Receive the message
                        partial(expect_stanza,
                         ("/message[@from='#foo%{irc_server_one}/{nick_one}'][@to='{jid_one}/{resource_one}'][@type='groupchat']/body[text()='coucou']",
                          "/message/stable_id:stanza-id[@by='#foo%{irc_server_one}'][@id]"),
                         ),

                         # Send a message from a resource that is not joined
                         partial(send_stanza, "<message from='{jid_one}/{resource_two}' to='#foo%{irc_server_one}' type='groupchat'><body>coucou</body></message>"),
                         partial(expect_stanza, ("/message[@type='error']/error[@type='modify']/stanza:text[text()='You are not a participant in this room.']",
                                                 "/message/error/stanza:not-acceptable"
                                                 ))

                         ]),
                Scenario("encoded_channel_join",
                 [
                     handshake_sequence(),
                     partial(send_stanza,
                             "<presence from='{jid_one}/{resource_one}' to='#biboumi\\40louiz.org\\3a80%{irc_server_one}/{nick_one}' />"),
                     connection_sequence("irc.localhost", '{jid_one}/{resource_one}'),
                     partial(expect_stanza,
                             "/message/body[text()='Mode #biboumi@louiz.org:80 [+nt] by {irc_host_one}']"),
                     partial(expect_stanza,
                             ("/presence[@to='{jid_one}/{resource_one}'][@from='#biboumi\\40louiz.org\\3a80%{irc_server_one}/{nick_one}']/muc_user:x/muc_user:item[@affiliation='admin'][@role='moderator']",
                             "/presence/muc_user:x/muc_user:status[@code='110']")
                             ),
                     partial(expect_stanza, "/message[@from='#biboumi\\40louiz.org\\3a80%{irc_server_one}'][@type='groupchat']/subject[not(text())]"),
                 ]),
                Scenario("self_ping_with_error",
                 [
                     handshake_sequence(),
                     partial(send_stanza,
                             "<presence from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}/{nick_one}' />"),
                     connection_sequence("irc.localhost", '{jid_one}/{resource_one}'),
                     partial(expect_stanza,
                             "/message/body[text()='Mode #foo [+nt] by {irc_host_one}']"),
                     partial(expect_stanza,
                             ("/presence[@to='{jid_one}/{resource_one}'][@from='#foo%{irc_server_one}/{nick_one}']/muc_user:x/muc_user:item[@affiliation='admin'][@role='moderator']",
                              "/presence/muc_user:x/muc_user:status[@code='110']")
                             ),
                     partial(expect_stanza, "/message[@from='#foo%{irc_server_one}'][@type='groupchat']/subject[not(text())]"),

                     # Send a ping to ourself
                     partial(send_stanza,
                             "<iq type='get' from='{jid_one}/{resource_one}' id='first_ping' to='#foo%{irc_server_one}/{nick_one}'><ping xmlns='urn:xmpp:ping' /></iq>"),
                     # We receive our own ping request,
                     partial(expect_stanza,
                             "/iq[@from='{lower_nick_one}%{irc_server_one}'][@type='get'][@to='{jid_one}/{resource_one}'][@id='gnip_tsrif']"),
                     # Respond to the request with an error
                     partial(send_stanza,
                             "<iq from='{jid_one}/{resource_one}' id='gnip_tsrif' to='{lower_nick_one}%{irc_server_one}' type='error'><error type='cancel'><feature-not-implemented xmlns='urn:ietf:params:xml:ns:xmpp-stanzas'/></error></iq>"),
                     partial(expect_stanza,
                             "/iq[@from='#foo%{irc_server_one}/{nick_one}'][@type='result'][@to='{jid_one}/{resource_one}'][@id='first_ping']"),

                     # Send a ping to ourself
                     partial(send_stanza,
                             "<iq type='get' from='{jid_one}/{resource_one}' id='first_ping' to='#foo%{irc_server_one}/{nick_one}'><ping xmlns='urn:xmpp:ping' /></iq>"),
                     # We receive our own ping request,
                     partial(expect_stanza,
                             "/iq[@from='{lower_nick_one}%{irc_server_one}'][@type='get'][@to='{jid_one}/{resource_one}'][@id='gnip_tsrif']"),
                     # Respond to the request with an error
                     partial(send_stanza,
                             "<iq from='{jid_one}/{resource_one}' id='gnip_tsrif' to='{lower_nick_one}%{irc_server_one}' type='error'><error type='cancel'><service-unavailable xmlns='urn:ietf:params:xml:ns:xmpp-stanzas'/></error></iq>"),
                     partial(expect_stanza,
                             "/iq[@from='#foo%{irc_server_one}/{nick_one}'][@type='result'][@to='{jid_one}/{resource_one}'][@id='first_ping']"),
                 ]),
                Scenario("self_ping_not_in_muc",
                 [
                     handshake_sequence(),
                     partial(send_stanza,
                             "<presence from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}/{nick_one}' />"),
                     connection_sequence("irc.localhost", '{jid_one}/{resource_one}'),
                     partial(expect_stanza,
                             "/message/body[text()='Mode #foo [+nt] by {irc_host_one}']"),
                     partial(expect_stanza,
                             ("/presence[@to='{jid_one}/{resource_one}'][@from='#foo%{irc_server_one}/{nick_one}']/muc_user:x/muc_user:item[@affiliation='admin'][@role='moderator']",
                              "/presence/muc_user:x/muc_user:status[@code='110']")
                             ),
                     partial(expect_stanza, "/message[@from='#foo%{irc_server_one}'][@type='groupchat']/subject[not(text())]"),

                     # Send a ping to ourself, in a muc where we’re not
                     partial(send_stanza,
                             "<iq type='get' from='{jid_one}/{resource_one}' id='first_ping' to='#nil%{irc_server_one}/{nick_one}'><ping xmlns='urn:xmpp:ping' /></iq>"),
                     # Immediately receive an error
                     partial(expect_stanza,
                             "/iq[@from='#nil%{irc_server_one}/{nick_one}'][@type='error'][@to='{jid_one}/{resource_one}'][@id='first_ping']/error/stanza:not-allowed"),

                     # Send a ping to ourself, in a muc where we are, but not this resource
                     partial(send_stanza,
                             "<iq type='get' from='{jid_one}/{resource_two}' id='first_ping' to='#foo%{irc_server_one}/{nick_one}'><ping xmlns='urn:xmpp:ping' /></iq>"),
                     # Immediately receive an error
                     partial(expect_stanza,
                             "/iq[@from='#foo%{irc_server_one}/{nick_one}'][@type='error'][@to='{jid_one}/{resource_two}'][@id='first_ping']/error/stanza:not-allowed"),
                 ]),
                Scenario("self_ping_on_real_channel",
                 [
                     handshake_sequence(),
                     partial(send_stanza,
                             "<presence from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}/{nick_one}' />"),
                     connection_sequence("irc.localhost", '{jid_one}/{resource_one}'),
                     partial(expect_stanza,
                             "/message/body[text()='Mode #foo [+nt] by {irc_host_one}']"),
                     partial(expect_stanza,
                             ("/presence[@to='{jid_one}/{resource_one}'][@from='#foo%{irc_server_one}/{nick_one}']/muc_user:x/muc_user:item[@affiliation='admin'][@role='moderator']",
                              "/presence/muc_user:x/muc_user:status[@code='110']")
                             ),
                     partial(expect_stanza, "/message[@from='#foo%{irc_server_one}'][@type='groupchat']/subject[not(text())]"),

                     # Send a ping to ourself
                     partial(send_stanza,
                             "<iq type='get' from='{jid_one}/{resource_one}' id='first_ping' to='#foo%{irc_server_one}/{nick_one}'><ping xmlns='urn:xmpp:ping' /></iq>"),
                     # We receive our own ping request,
                     partial(expect_stanza,
                             "/iq[@from='{lower_nick_one}%{irc_server_one}'][@type='get'][@to='{jid_one}/{resource_one}'][@id='gnip_tsrif']"),
                     # Respond to the request
                     partial(send_stanza,
                             "<iq type='result' to='{lower_nick_one}%{irc_server_one}' id='gnip_tsrif' from='{jid_one}/{resource_one}'/>"),
                     partial(expect_stanza,
                             "/iq[@from='#foo%{irc_server_one}/{nick_one}'][@type='result'][@to='{jid_one}/{resource_one}'][@id='first_ping']"),

                     # Now join the same room, from the same bare JID, behind the same nick
                     partial(send_stanza,
                             "<presence from='{jid_one}/{resource_two}' to='#foo%{irc_server_one}/{nick_one}' />"),
                     partial(expect_stanza,
                             ("/presence[@to='{jid_one}/{resource_two}'][@from='#foo%{irc_server_one}/{nick_one}']/muc_user:x/muc_user:item[@affiliation='admin'][@role='moderator']",
                              "/presence/muc_user:x/muc_user:status[@code='110']")
                             ),
                     partial(expect_stanza, "/message[@from='#foo%{irc_server_one}'][@type='groupchat'][@to='{jid_one}/{resource_two}']/subject[not(text())]"),

                     # And re-send a self ping
                     partial(send_stanza,
                             "<iq type='get' from='{jid_one}/{resource_one}' id='second_ping' to='#foo%{irc_server_one}/{nick_one}'><ping xmlns='urn:xmpp:ping' /></iq>"),
                     # We receive our own ping request. Note that we don't know the to value, it could be one of our two resources.
                     partial(expect_stanza,
                             "/iq[@from='{lower_nick_one}%{irc_server_one}'][@type='get'][@to][@id='gnip_dnoces']",
                             after = partial(save_value, "to", partial(extract_attribute, "/iq", "to"))),
                     # Respond to the request, using the extracted 'to' value as our 'from'
                     partial(send_stanza,
                             "<iq type='result' to='{lower_nick_one}%{irc_server_one}' id='gnip_dnoces' from='{to}'/>"),
                     partial(expect_stanza,
                             "/iq[@from='#foo%{irc_server_one}/{nick_one}'][@type='result'][@to='{jid_one}/{resource_one}'][@id='second_ping']"),
                     ## And re-do exactly the same thing, just change the resource initiating the self ping
                     partial(send_stanza,
                             "<iq type='get' from='{jid_one}/{resource_two}' id='third_ping' to='#foo%{irc_server_one}/{nick_one}'><ping xmlns='urn:xmpp:ping' /></iq>"),
                     partial(expect_stanza,
                             "/iq[@from='{lower_nick_one}%{irc_server_one}'][@type='get'][@to][@id='gnip_driht']",
                             after = partial(save_value, "to", partial(extract_attribute, "/iq", "to"))),
                     partial(send_stanza,
                             "<iq type='result' to='{lower_nick_one}%{irc_server_one}' id='gnip_driht' from='{to}'/>"),
                     partial(expect_stanza,
                             "/iq[@from='#foo%{irc_server_one}/{nick_one}'][@type='result'][@to='{jid_one}/{resource_two}'][@id='third_ping']"),

                 ]),
               Scenario("self_ping_fixed_server", [
                     handshake_sequence(),
                     partial(send_stanza,
                             "<presence from='{jid_one}/{resource_one}' to='#foo@{biboumi_host}/{nick_one}' />"),
                     connection_sequence("irc.localhost", '{jid_one}/{resource_one}', fixed_irc_server=True),
                     partial(expect_stanza,
                             "/message/body[text()='Mode #foo [+nt] by {irc_host_one}']"),
                     partial(expect_stanza,
                            ("/presence[@to='{jid_one}/{resource_one}'][@from='#foo@{biboumi_host}/{nick_one}']/muc_user:x/muc_user:item[@affiliation='admin'][@role='moderator']",
                             "/presence/muc_user:x/muc_user:status[@code='110']")
                             ),
                     partial(expect_stanza, "/message[@from='#foo@{biboumi_host}'][@type='groupchat']/subject[not(text())]"),

                     # Send a ping to ourself
                     partial(send_stanza,
                             "<iq type='get' from='{jid_one}/{resource_one}' id='first_ping' to='#foo@{biboumi_host}/{nick_one}'><ping xmlns='urn:xmpp:ping' /></iq>"),
                     # We receive our own ping request,
                     partial(expect_stanza,
                             "/iq[@from='{lower_nick_one}@{biboumi_host}'][@type='get'][@to='{jid_one}/{resource_one}'][@id='gnip_tsrif']"),
                     # Respond to the request
                     partial(send_stanza,
                     "<iq type='result' to='{lower_nick_one}@{biboumi_host}' id='gnip_tsrif' from='{jid_one}/{resource_one}'/>"),
                     partial(expect_stanza,
                     "/iq[@from='#foo@{biboumi_host}/{nick_one}'][@type='result'][@to='{jid_one}/{resource_one}'][@id='first_ping']"),
                ], conf="fixed_server"),
                Scenario("simple_kick",
                [
                     handshake_sequence(),
                     # First user joins
                     partial(send_stanza,
                     "<presence from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}/{nick_one}' />"),
                     connection_sequence("irc.localhost", '{jid_one}/{resource_one}'),
                     partial(expect_stanza, "/message"),
                     partial(expect_stanza, "/presence/muc_user:x/muc_user:status[@code='110']"),
                     partial(expect_stanza, "/message[@type='groupchat']/subject"),

                     # Second user joins
                     partial(send_stanza,
                     "<presence from='{jid_two}/{resource_one}' to='#foo%{irc_server_one}/{nick_two}' />"),
                     connection_sequence("irc.localhost", '{jid_two}/{resource_one}'),
                     partial(expect_unordered, [
                         ("/presence[@to='{jid_one}/{resource_one}']/muc_user:x/muc_user:item[@affiliation='none'][@role='participant']",),
                         ("/presence[@to='{jid_two}/{resource_one}']/muc_user:x/muc_user:item[@affiliation='none'][@role='participant']",
                          "/presence/muc_user:x/muc_user:status[@code='110']"),
                         ("/presence[@to='{jid_two}/{resource_one}']/muc_user:x/muc_user:item[@affiliation='admin'][@role='moderator']",),
                         ("/message/subject",),
                         ]),

                     # demonstrate bug https://lab.louiz.org/louiz/biboumi/issues/3291
                     # First user joins an other channel
                     partial(send_stanza,
                     "<presence from='{jid_one}/{resource_one}' to='#bar%{irc_server_one}/{nick_one}' />"),
                     partial(expect_stanza, "/message"),
                     partial(expect_stanza, "/presence/muc_user:x/muc_user:status[@code='110']"),
                     partial(expect_stanza, "/message[@type='groupchat']/subject"),

                     # Second user joins
                     partial(send_stanza,
                     "<presence from='{jid_two}/{resource_one}' to='#bar%{irc_server_one}/{nick_two}' />"),
                     partial(expect_unordered, [
                         ("/presence[@to='{jid_one}/{resource_one}']/muc_user:x/muc_user:item[@affiliation='none'][@role='participant']",),
                         ("/presence[@to='{jid_two}/{resource_one}']/muc_user:x/muc_user:item[@affiliation='none'][@role='participant']",
                          "/presence/muc_user:x/muc_user:status[@code='110']"),
                         ("/presence[@to='{jid_two}/{resource_one}']/muc_user:x/muc_user:item[@affiliation='admin'][@role='moderator']",),
                         ("/message/subject",),
                         ]),

                     # Moderator kicks participant
                     partial(send_stanza,
                     "<iq id='kick1' to='#foo%{irc_server_one}' from='{jid_one}/{resource_one}' type='set'><query xmlns='http://jabber.org/protocol/muc#admin'><item nick='{nick_two}' role='none'><reason>reported</reason></item></query></iq>"),
                     partial(expect_unordered, [
                             ("/presence[@type='unavailable'][@to='{jid_two}/{resource_one}']/muc_user:x/muc_user:item[@role='none']/muc_user:actor[@nick='{nick_one}']",
                              "/presence/muc_user:x/muc_user:item/muc_user:reason[text()='reported']",
                              "/presence/muc_user:x/muc_user:status[@code='307']",
                              "/presence/muc_user:x/muc_user:status[@code='110']"
                             ),
                             ("/presence[@type='unavailable'][@to='{jid_one}/{resource_one}']/muc_user:x/muc_user:item[@role='none']/muc_user:actor[@nick='{nick_one}']",
                              "/presence/muc_user:x/muc_user:item/muc_user:reason[text()='reported']",
                              "/presence/muc_user:x/muc_user:status[@code='307']",
                              ),
                             ("/iq[@id='kick1'][@type='result']",),
                     ]),

                    # Bug 3291, suite. We must not receive any presence from #foo, here
                    partial(send_stanza, "<message from='{jid_two}/{resource_one}' to='{irc_server_one}' type='chat'><body>QUIT bye bye</body></message>"),
                    partial(expect_unordered,
                            [("/presence[@from='#bar%{irc_server_one}/{nick_two}'][@to='{jid_one}/{resource_one}']",),
                             ("/presence[@from='#bar%{irc_server_one}/{nick_two}'][@to='{jid_two}/{resource_one}']",),
                             ("/message",),
                             ("/message",)])
                ]),
        Scenario("mode_change",
                [
                     handshake_sequence(),
                     # First user joins
                     partial(send_stanza,
                     "<presence from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}/{nick_one}' />"),
                     connection_sequence("irc.localhost", '{jid_one}/{resource_one}'),
                     partial(expect_stanza, "/message"),
                     partial(expect_stanza, "/presence/muc_user:x/muc_user:status[@code='110']"),
                     partial(expect_stanza, "/message[@type='groupchat']/subject"),

                     # Second user joins
                     partial(send_stanza,
                     "<presence from='{jid_two}/{resource_one}' to='#foo%{irc_server_one}/{nick_two}' />"),
                     connection_sequence("irc.localhost", '{jid_two}/{resource_one}'),
                     partial(expect_unordered, [
                         ("/presence/muc_user:x/muc_user:item[@affiliation='none'][@role='participant']",),
                         ("/presence/muc_user:x/muc_user:item[@affiliation='admin'][@role='moderator']",),
                         ("/presence/muc_user:x/muc_user:status[@code='110']",),
                         ("/message/subject",),
                         ]),

                     # Change a user mode with a message starting with /mode
                    partial(send_stanza,
                            "<message from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}' type='groupchat'><body>/mode +v {nick_two}</body></message>"),
                    partial(expect_unordered, [
                        ("/message[@to='{jid_one}/{resource_one}']/body[text()='Mode #foo [+v {nick_two}] by {nick_one}']",),
                        ("/message[@to='{jid_two}/{resource_one}']/body[text()='Mode #foo [+v {nick_two}] by {nick_one}']",),
                        ("/presence[@to='{jid_two}/{resource_one}'][@from='#foo%{irc_server_one}/{nick_two}']/muc_user:x/muc_user:item[@affiliation='member'][@role='participant']",),
                        ("/presence[@to='{jid_one}/{resource_one}'][@from='#foo%{irc_server_one}/{nick_two}']/muc_user:x/muc_user:item[@affiliation='member'][@role='participant']",)
                        ]),

                    # using an iq
                    partial(send_stanza,
                            "<iq from='{jid_one}/{resource_one}' id='id1' to='#foo%{irc_server_one}' type='set'><query xmlns='http://jabber.org/protocol/muc#admin'><item affiliation='admin' nick='{nick_two}'/></query></iq>"),
                    partial(expect_unordered, [
                        ("/message[@to='{jid_one}/{resource_one}']/body[text()='Mode #foo [+o {nick_two}] by {nick_one}']",),
                        ("/message[@to='{jid_two}/{resource_one}']/body[text()='Mode #foo [+o {nick_two}] by {nick_one}']",),
                        ("/presence[@to='{jid_two}/{resource_one}'][@from='#foo%{irc_server_one}/{nick_two}']/muc_user:x/muc_user:item[@affiliation='admin'][@role='moderator']",),
                        ("/presence[@to='{jid_one}/{resource_one}'][@from='#foo%{irc_server_one}/{nick_two}']/muc_user:x/muc_user:item[@affiliation='admin'][@role='moderator']",),
                        ("/iq[@id='id1'][@type='result'][@to='{jid_one}/{resource_one}'][@from='#foo%{irc_server_one}']",),
                        ]),

                    # remove the mode
                    partial(send_stanza,
                            "<iq from='{jid_one}/{resource_one}' id='id1' to='#foo%{irc_server_one}' type='set'><query xmlns='http://jabber.org/protocol/muc#admin'><item affiliation='member' nick='{nick_two}' role='participant'/></query></iq>"),
                    partial(expect_unordered, [
                        ("/message[@to='{jid_one}/{resource_one}']/body[text()='Mode #foo [+v-o {nick_two} {nick_two}] by {nick_one}']",),
                        ("/message[@to='{jid_two}/{resource_one}']/body[text()='Mode #foo [+v-o {nick_two} {nick_two}] by {nick_one}']",),
                        ("/presence[@to='{jid_two}/{resource_one}'][@from='#foo%{irc_server_one}/{nick_two}']/muc_user:x/muc_user:item[@affiliation='member'][@role='participant']",),
                        ("/presence[@to='{jid_one}/{resource_one}'][@from='#foo%{irc_server_one}/{nick_two}']/muc_user:x/muc_user:item[@affiliation='member'][@role='participant']",),
                        ("/iq[@id='id1'][@type='result'][@to='{jid_one}/{resource_one}'][@from='#foo%{irc_server_one}']",),
                        ]),

                    # using an iq, an a non-existant nick
                    partial(send_stanza,
                            "<iq from='{jid_one}/{resource_one}' id='id1' to='#foo%{irc_server_one}' type='set'><query xmlns='http://jabber.org/protocol/muc#admin'><item affiliation='admin' nick='blectre'/></query></iq>"),
                    partial(expect_stanza, "/iq[@type='error']"),

                    # using an iq, without the rights to do it
                    partial(send_stanza,
                            "<iq from='{jid_two}/{resource_one}' id='id1' to='#foo%{irc_server_one}' type='set'><query xmlns='http://jabber.org/protocol/muc#admin'><item affiliation='admin' nick='{nick_one}'/></query></iq>"),
                    partial(expect_unordered, [
                        ("/iq[@type='error']",),
                        ("/message[@type='chat'][@to='{jid_two}/{resource_one}']",),
                    ]),

                    # using an iq, with an unknown mode
                    partial(send_stanza,
                            "<iq from='{jid_two}/{resource_one}' id='id1' to='#foo%{irc_server_one}' type='set'><query xmlns='http://jabber.org/protocol/muc#admin'><item affiliation='owner' nick='{nick_one}'/></query></iq>"),
                    partial(expect_unordered, [
                        ("/iq[@type='error']",),
                        ("/message[@type='chat'][@to='{jid_two}/{resource_one}']",),
                    ]),

                ]),
                Scenario("multisession_kick",
                 [
                     handshake_sequence(),
                     # First user joins
                     partial(send_stanza,
                             "<presence from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}/{nick_one}' />"),
                     connection_sequence("irc.localhost", '{jid_one}/{resource_one}'),
                     partial(expect_stanza, "/message"),
                     partial(expect_stanza, "/presence/muc_user:x/muc_user:status[@code='110']"),
                     partial(expect_stanza, "/message[@type='groupchat']/subject"),

                     # Second user joins, from two resources
                     partial(send_stanza,
                             "<presence from='{jid_two}/{resource_one}' to='#foo%{irc_server_one}/{nick_two}' />"),
                     connection_sequence("irc.localhost", '{jid_two}/{resource_one}'),
                     partial(expect_unordered, [
                         ("/presence/muc_user:x/muc_user:item[@affiliation='none'][@role='participant']",),
                         ("/presence/muc_user:x/muc_user:item[@affiliation='admin'][@role='moderator']",),
                         ("/presence/muc_user:x/muc_user:status[@code='110']",),
                         ("/message/subject",),
                         ]),

                     partial(send_stanza,
                             "<presence from='{jid_two}/{resource_two}' to='#foo%{irc_server_one}/{nick_two}' />"),
                     partial(expect_stanza,
                             "/presence[@to='{jid_two}/{resource_two}'][@from='#foo%{irc_server_one}/{nick_one}']"),
                     partial(expect_stanza,
                             ("/presence[@to='{jid_two}/{resource_two}'][@from='#foo%{irc_server_one}/{nick_two}']",
                              "/presence/muc_user:x/muc_user:status[@code='110']")
                             ),
                     partial(expect_stanza, "/message[@from='#foo%{irc_server_one}'][@type='groupchat'][@to='{jid_two}/{resource_two}']/subject[not(text())]"),

                     # Moderator kicks participant
                     partial(send_stanza,
                             "<iq id='kick1' to='#foo%{irc_server_one}' from='{jid_one}/{resource_one}' type='set'><query xmlns='http://jabber.org/protocol/muc#admin'><item nick='{nick_two}' role='none'><reason>reported</reason></item></query></iq>"),
                     partial(expect_unordered, [
                             ("/presence[@type='unavailable'][@to='{jid_two}/{resource_one}']/muc_user:x/muc_user:item[@role='none']/muc_user:actor[@nick='{nick_one}']",
                              "/presence/muc_user:x/muc_user:item/muc_user:reason[text()='reported']",
                              "/presence/muc_user:x/muc_user:status[@code='307']",
                              "/presence/muc_user:x/muc_user:status[@code='110']"
                              ),
                             ("/presence[@type='unavailable'][@to='{jid_two}/{resource_two}']/muc_user:x/muc_user:item[@role='none']/muc_user:actor[@nick='{nick_one}']",
                              "/presence/muc_user:x/muc_user:item/muc_user:reason[text()='reported']",
                              "/presence/muc_user:x/muc_user:status[@code='307']",
                              "/presence/muc_user:x/muc_user:status[@code='110']"
                              ),
                             ("/presence[@type='unavailable']/muc_user:x/muc_user:item[@role='none']/muc_user:actor[@nick='{nick_one}']",
                              "/presence/muc_user:x/muc_user:item/muc_user:reason[text()='reported']",
                              "/presence/muc_user:x/muc_user:status[@code='307']",
                              ),
                             ("/iq[@id='kick1'][@type='result']",),
                             ]),
                 ]),
                Scenario("self_version",
                 [
                     handshake_sequence(),
                     partial(send_stanza,
                             "<presence from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}/{nick_one}' />"),
                     connection_sequence("irc.localhost", '{jid_one}/{resource_one}'),
                     partial(expect_stanza,
                             "/message/body[text()='Mode #foo [+nt] by {irc_host_one}']"),
                     partial(expect_stanza,
                             ("/presence[@to='{jid_one}/{resource_one}'][@from='#foo%{irc_server_one}/{nick_one}']/muc_user:x/muc_user:item[@affiliation='admin'][@role='moderator']",
                              "/presence/muc_user:x/muc_user:status[@code='110']")
                             ),
                     partial(expect_stanza, "/message[@from='#foo%{irc_server_one}'][@type='groupchat']/subject[not(text())]"),

                     # Send a version request to ourself
                     partial(send_stanza,
                             "<iq type='get' from='{jid_one}/{resource_one}' id='first_version' to='#foo%{irc_server_one}/{nick_one}'><query xmlns='jabber:iq:version' /></iq>"),
                     # We receive our own request,
                     partial(expect_stanza,
                             "/iq[@from='{lower_nick_one}%{irc_server_one}'][@type='get'][@to='{jid_one}/{resource_one}']",
                             after = partial(save_value, "id", partial(extract_attribute, "/iq", 'id'))),
                     # Respond to the request
                     partial(send_stanza,
                             "<iq type='result' to='{lower_nick_one}%{irc_server_one}' id='{id}' from='{jid_one}/{resource_one}'><query xmlns='jabber:iq:version'><name>e2e test</name><version>1.0</version><os>Fedora</os></query></iq>"),
                     partial(expect_stanza,
                             "/iq[@from='#foo%{irc_server_one}/{nick_one}'][@type='result'][@to='{jid_one}/{resource_one}'][@id='first_version']/version:query/version:name[text()='e2e test (through the biboumi gateway) 1.0 Fedora']"),

                     # Now join the same room, from the same bare JID, behind the same nick
                     partial(send_stanza,
                             "<presence from='{jid_one}/{resource_two}' to='#foo%{irc_server_one}/{nick_one}' />"),
                     partial(expect_stanza,
                             ("/presence[@to='{jid_one}/{resource_two}'][@from='#foo%{irc_server_one}/{nick_one}']/muc_user:x/muc_user:item[@affiliation='admin'][@role='moderator']",
                              "/presence/muc_user:x/muc_user:status[@code='110']")
                             ),
                     partial(expect_stanza, "/message[@from='#foo%{irc_server_one}'][@type='groupchat'][@to='{jid_one}/{resource_two}']/subject[not(text())]"),

                     # And re-send a self ping
                     partial(send_stanza,
                             "<iq type='get' from='{jid_one}/{resource_two}' id='second_version' to='#foo%{irc_server_one}/{nick_one}'><query xmlns='jabber:iq:version' /></iq>"),
                     # We receive our own request. Note that we don't know the to value, it could be one of our two resources.
                     partial(expect_stanza,
                             "/iq[@from='{lower_nick_one}%{irc_server_one}'][@type='get'][@to]",
                             after = (partial(save_value, "to", partial(extract_attribute, "/iq", "to")),
                                      partial(save_value, "id", partial(extract_attribute, "/iq", "id")))),
                     # Respond to the request, using the extracted 'to' value as our 'from'
                     partial(send_stanza,
                             "<iq type='result' to='{lower_nick_one}%{irc_server_one}' id='{id}' from='{to}'><query xmlns='jabber:iq:version'><name>e2e test</name><version>1.0</version><os>Fedora</os></query></iq>"),
                     partial(expect_stanza,
                             "/iq[@from='#foo%{irc_server_one}/{nick_one}'][@type='result'][@to='{jid_one}/{resource_two}'][@id='second_version']"),

                     # And do exactly the same thing, but initiated by the other resource
                     partial(send_stanza,
                             "<iq type='get' from='{jid_one}/{resource_one}' id='second_version' to='#foo%{irc_server_one}/{nick_one}'><query xmlns='jabber:iq:version' /></iq>"),
                     # We receive our own request. Note that we don't know the to value, it could be one of our two resources.
                     partial(expect_stanza,
                             "/iq[@from='{lower_nick_one}%{irc_server_one}'][@type='get'][@to]",
                             after = (partial(save_value, "to", partial(extract_attribute, "/iq", "to")),
                                      partial(save_value, "id", partial(extract_attribute, "/iq", "id")))),
                     # Respond to the request, using the extracted 'to' value as our 'from'
                     partial(send_stanza,
                             "<iq type='result' to='{lower_nick_one}%{irc_server_one}' id='{id}' from='{to}'><query xmlns='jabber:iq:version'><name>e2e test</name><version>1.0</version><os>Fedora</os></query></iq>"),
                     partial(expect_stanza,
                             "/iq[@from='#foo%{irc_server_one}/{nick_one}'][@type='result'][@to='{jid_one}/{resource_one}'][@id='second_version']"),
                 ]),
                Scenario("version_on_global_nick",
                [
                    handshake_sequence(),
                    partial(send_stanza,
                            "<presence from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}/{nick_one}' />"),
                    connection_sequence("irc.localhost", '{jid_one}/{resource_one}'),
                    partial(expect_stanza,
                            "/message/body[text()='Mode #foo [+nt] by {irc_host_one}']"),
                    partial(expect_stanza,
                            ("/presence[@to='{jid_one}/{resource_one}'][@from='#foo%{irc_server_one}/{nick_one}']/muc_user:x/muc_user:item[@affiliation='admin'][@role='moderator']",
                             "/presence/muc_user:x/muc_user:status[@code='110']")
                            ),
                    partial(expect_stanza, "/message[@from='#foo%{irc_server_one}'][@type='groupchat']/subject[not(text())]"),

                     partial(send_stanza,
                             "<iq type='get' from='{jid_one}/{resource_one}' id='first_version' to='{lower_nick_one}%{irc_server_one}'><query xmlns='jabber:iq:version' /></iq>"),

                     partial(expect_stanza,
                             "/iq[@from='{lower_nick_one}%{irc_server_one}'][@type='get'][@to='{jid_one}/{resource_one}']",
                             after = partial(save_value, "id", partial(extract_attribute, "/iq", 'id'))),
                     partial(send_stanza,
                             "<iq type='result' to='{lower_nick_one}%{irc_server_one}' id='{id}' from='{jid_one}/{resource_one}'><query xmlns='jabber:iq:version'><name>e2e test</name><version>1.0</version><os>Fedora</os></query></iq>"),
                     partial(expect_stanza,
                             "/iq[@from='{lower_nick_one}%{irc_server_one}'][@type='result'][@to='{jid_one}/{resource_one}'][@id='first_version']/version:query/version:name[text()='e2e test (through the biboumi gateway) 1.0 Fedora']"),

                ]),
                Scenario("self_invite",
                [
                    handshake_sequence(),
                    partial(send_stanza,
                            "<presence from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}/{nick_one}' />"),
                    connection_sequence("irc.localhost", '{jid_one}/{resource_one}'),
                    partial(expect_stanza,
                            "/message/body[text()='Mode #foo [+nt] by {irc_host_one}']"),
                    partial(expect_stanza,
                            ("/presence[@to='{jid_one}/{resource_one}'][@from='#foo%{irc_server_one}/{nick_one}']/muc_user:x/muc_user:item[@affiliation='admin'][@role='moderator']",
                             "/presence/muc_user:x/muc_user:status[@code='110']")
                            ),
                    partial(expect_stanza, "/message[@from='#foo%{irc_server_one}'][@type='groupchat']/subject[not(text())]"),
                    partial(send_stanza,
                            "<message from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}'><x xmlns='http://jabber.org/protocol/muc#user'><invite to='{nick_one}'/></x></message>"),
                    partial(expect_stanza,
                            "/message/body[text()='{nick_one} is already on channel #foo']")
                ]),
                Scenario("client_error",
                [
                    handshake_sequence(),
                    # First resource
                    partial(send_stanza,
                            "<presence from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}/{nick_one}' />"),
                    connection_sequence("irc.localhost", '{jid_one}/{resource_one}'),
                    partial(expect_stanza,
                            "/message/body[text()='Mode #foo [+nt] by {irc_host_one}']"),
                    partial(expect_stanza,
                            ("/presence[@to='{jid_one}/{resource_one}'][@from='#foo%{irc_server_one}/{nick_one}']/muc_user:x/muc_user:item[@affiliation='admin'][@role='moderator']",
                             "/presence/muc_user:x/muc_user:status[@code='110']")
                            ),
                    partial(expect_stanza, "/message[@from='#foo%{irc_server_one}'][@type='groupchat']/subject[not(text())]"),

                    # Second resource, same channel
                    partial(send_stanza,
                            "<presence from='{jid_one}/{resource_two}' to='#foo%{irc_server_one}/{nick_one}' />"),
                    partial(expect_stanza,
                            ("/presence[@to='{jid_one}/{resource_two}'][@from='#foo%{irc_server_one}/{nick_one}']/muc_user:x/muc_user:item[@affiliation='admin'][@role='moderator']",
                             "/presence/muc_user:x/muc_user:status[@code='110']")
                            ),
                    partial(expect_stanza, "/message[@from='#foo%{irc_server_one}'][@type='groupchat'][@to='{jid_one}/{resource_two}']/subject[not(text())]"),

                    # Now the first resource has an error
                    partial(send_stanza,
                            "<message from='{jid_one}/{resource_one}' to='#foo%%{irc_server_one}/{nick_one}' type='error'><error type='cancel'><recipient-unavailable xmlns='urn:ietf:params:xml:ns:xmpp-stanzas'/></error></message>"),
                    # Receive a leave only to the leaving resource
                    partial(expect_stanza,
                            ("/presence[@type='unavailable'][@from='#foo%{irc_server_one}/{nick_one}'][@to='{jid_one}/{resource_one}']/muc_user:x/muc_user:status[@code='110']",
                             "/presence/status[text()='Biboumi note: 1 resources are still in this channel.']")
                            ),
                ]),
                Scenario("simple_mam",
                [
                    handshake_sequence(),
                    partial(send_stanza,
                            "<presence from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}/{nick_one}' />"),
                    connection_sequence("irc.localhost", '{jid_one}/{resource_one}'),
                    partial(expect_stanza,
                            "/message/body[text()='Mode #foo [+nt] by {irc_host_one}']"),
                    partial(expect_stanza,
                            ("/presence[@to='{jid_one}/{resource_one}'][@from='#foo%{irc_server_one}/{nick_one}']/muc_user:x/muc_user:item[@affiliation='admin'][@role='moderator']",
                             "/presence/muc_user:x/muc_user:status[@code='110']")
                            ),
                    partial(expect_stanza, "/message[@from='#foo%{irc_server_one}'][@type='groupchat']/subject[not(text())]"),

                    # Send two channel messages
                    partial(send_stanza, "<message from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}' type='groupchat'><body>coucou</body></message>"),
                    partial(expect_stanza,
                            ("/message[@from='#foo%{irc_server_one}/{nick_one}'][@to='{jid_one}/{resource_one}'][@type='groupchat']/body[text()='coucou']",
                            "/message/stable_id:stanza-id[@by='#foo%{irc_server_one}'][@id]",)
                            ),

                    partial(send_stanza, "<message from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}' type='groupchat'><body>coucou 2</body></message>"),
                    partial(expect_stanza, "/message[@from='#foo%{irc_server_one}/{nick_one}'][@to='{jid_one}/{resource_one}'][@type='groupchat']/body[text()='coucou 2']"),

                    # Retrieve the complete archive
                    partial(send_stanza, "<iq to='#foo%{irc_server_one}' from='{jid_one}/{resource_one}' type='set' id='id1'><query xmlns='urn:xmpp:mam:2' queryid='qid1' /></iq>"),

                    partial(expect_stanza,
                            ("/message/mam:result[@queryid='qid1']/forward:forwarded/delay:delay",
                            "/message/mam:result[@queryid='qid1']/forward:forwarded/client:message[@from='#foo%{irc_server_one}/{nick_one}'][@type='groupchat']/client:body[text()='coucou']")
                            ),
                    partial(expect_stanza,
                            ("/message/mam:result[@queryid='qid1']/forward:forwarded/delay:delay",
                            "/message/mam:result[@queryid='qid1']/forward:forwarded/client:message[@from='#foo%{irc_server_one}/{nick_one}'][@type='groupchat']/client:body[text()='coucou 2']")
                            ),

                    partial(expect_stanza,
                            ("/iq[@type='result'][@id='id1'][@from='#foo%{irc_server_one}'][@to='{jid_one}/{resource_one}']",
                             "/iq/mam:fin/rms:set/rsm:last",
                             "/iq/mam:fin/rsm:set/rsm:first",
                             "/iq/mam:fin[@complete='true']")),

                    # Retrieve an empty archive by specifying an early “end” date
                    partial(send_stanza, """<iq to='#foo%{irc_server_one}' from='{jid_one}/{resource_one}' type='set' id='id2'>
                    <query xmlns='urn:xmpp:mam:2' queryid='qid2'>
                    <x xmlns='jabber:x:data' type='submit'>
                    <field var='FORM_TYPE' type='hidden'> <value>urn:xmpp:mam:2</value></field>
                    <field var='end'><value>2000-06-07T00:00:00Z</value></field>
                    </x>
                    </query></iq>"""),

                    partial(expect_stanza,
                            ("/iq[@type='result'][@id='id2'][@from='#foo%{irc_server_one}'][@to='{jid_one}/{resource_one}']",
                             "/iq/mam:fin[@complete='true']/rsm:set",)),

                    # Retrieve an empty archive by specifying a late “start” date
                    # (note that this test will break in ~1000 years)
                    partial(send_stanza, """<iq to='#foo%{irc_server_one}' from='{jid_one}/{resource_one}' type='set' id='id3'>
                    <query xmlns='urn:xmpp:mam:2' queryid='qid3'>
                    <x xmlns='jabber:x:data' type='submit'>
                    <field var='FORM_TYPE' type='hidden'> <value>urn:xmpp:mam:2</value></field>
                    <field var='start'><value>3016-06-07T00:00:00Z</value></field>
                    </x>
                    </query></iq>"""),

                    partial(expect_stanza,
                            ("/iq[@type='result'][@id='id3'][@from='#foo%{irc_server_one}'][@to='{jid_one}/{resource_one}']",
                             "/iq/mam:fin[@complete='true']/rsm:set")),

                    # Retrieve the whole archive, but limit the response to one elemet
                    partial(send_stanza, "<iq to='#foo%{irc_server_one}' from='{jid_one}/{resource_one}' type='set' id='id4'><query xmlns='urn:xmpp:mam:2' queryid='qid4'><set xmlns='http://jabber.org/protocol/rsm'><max>1</max></set></query></iq>"),

                    partial(expect_stanza,
                            ("/message/mam:result[@queryid='qid4']/forward:forwarded/delay:delay",
                             "/message/mam:result[@queryid='qid4']/forward:forwarded/client:message[@from='#foo%{irc_server_one}/{nick_one}'][@type='groupchat']/client:body[text()='coucou']")
                            ),

                    partial(expect_stanza,
                            ("/iq[@type='result'][@id='id4'][@from='#foo%{irc_server_one}'][@to='{jid_one}/{resource_one}']",
                             "!/iq/mam:fin[@complete='true']/rsm:set")),

                ]),
        Scenario("mam_with_timestamps",
                 [
                     handshake_sequence(),
                     partial(send_stanza,
                             "<presence from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}/{nick_one}' />"),
                     connection_sequence("irc.localhost", '{jid_one}/{resource_one}'),
                     partial(expect_stanza,
                             "/message/body[text()='Mode #foo [+nt] by {irc_host_one}']"),
                     partial(expect_stanza,
                             ("/presence[@to='{jid_one}/{resource_one}'][@from='#foo%{irc_server_one}/{nick_one}']/muc_user:x/muc_user:item[@affiliation='admin'][@role='moderator']",
                              "/presence/muc_user:x/muc_user:status[@code='110']")
                             ),
                     partial(expect_stanza, "/message[@from='#foo%{irc_server_one}'][@type='groupchat']/subject[not(text())]"),

                     # Send two channel messages
                     partial(send_stanza, "<message from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}' type='groupchat'><body>coucou</body></message>"),
                     partial(expect_stanza,
                             ("/message[@from='#foo%{irc_server_one}/{nick_one}'][@to='{jid_one}/{resource_one}'][@type='groupchat']/body[text()='coucou']",
                              "/message/stable_id:stanza-id[@by='#foo%{irc_server_one}'][@id]",)
                             ),

                     partial(send_stanza, "<message from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}' type='groupchat'><body>coucou 2</body></message>"),
                     # Record the current time
                     partial(expect_stanza, "/message[@from='#foo%{irc_server_one}/{nick_one}'][@to='{jid_one}/{resource_one}'][@type='groupchat']/body[text()='coucou 2']",
                             after = partial(save_current_timestamp_plus_delta, "first_timestamp", datetime.timedelta(seconds=1))),

                     # Wait two seconds before sending two new messages
                     partial(sleep_for, 2),
                     partial(send_stanza, "<message from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}' type='groupchat'><body>coucou 3</body></message>"),
                     partial(send_stanza, "<message from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}' type='groupchat'><body>coucou 4</body></message>"),
                     partial(expect_stanza, "/message[@type='groupchat']/body[text()='coucou 3']"),
                     partial(expect_stanza, "/message[@type='groupchat']/body[text()='coucou 4']",
                             after = partial(save_current_timestamp_plus_delta, "second_timestamp", datetime.timedelta(seconds=1))),

                     # Retrieve the archive, after our saved datetime
                     partial(send_stanza, """<iq to='#foo%{irc_server_one}' from='{jid_one}/{resource_one}' type='set' id='id8'>
                          <query xmlns='urn:xmpp:mam:2' queryid='qid16'>
                            <x type='submit' xmlns='jabber:x:data'>
                             <field var='FORM_TYPE' xmlns='jabber:x:data'><value xmlns='jabber:x:data'>urn:xmpp:mam:2</value></field>
                             <field var='start' xmlns='jabber:x:data'><value xmlns='jabber:x:data'>{first_timestamp}</value></field>
                             <field var='end' xmlns='jabber:x:data'><value xmlns='jabber:x:data'>{second_timestamp}</value></field>
                            </x>
                          </query>
                         </iq>"""),


                     partial(expect_stanza,
                             ("/message/mam:result[@queryid='qid16']/forward:forwarded/delay:delay",
                              "/message/mam:result/forward:forwarded/client:message[@from='#foo%{irc_server_one}/{nick_one}'][@type='groupchat']/client:body[text()='coucou 3']")
                             ),

                     partial(expect_stanza,
                             ("/message/mam:result[@queryid='qid16']/forward:forwarded/delay:delay",
                              "/message/mam:result/forward:forwarded/client:message[@from='#foo%{irc_server_one}/{nick_one}'][@type='groupchat']/client:body[text()='coucou 4']")
                             ),

                     partial(expect_stanza,
                             ("/iq[@type='result'][@id='id8'][@from='#foo%{irc_server_one}'][@to='{jid_one}/{resource_one}']",
                              "/iq/mam:fin[@complete='true']/rsm:set")),
                 ]),
        Scenario("join_history_limits",
                 [
                     handshake_sequence(),

                     # Disable the throttling because the test is based on timings
                     partial(send_stanza, "<iq type='set' id='id1' from='{jid_one}/{resource_one}' to='{irc_server_one}'><command xmlns='http://jabber.org/protocol/commands' node='configure' action='execute' /></iq>"),
                     partial(expect_stanza, "/iq[@type='result']",
                             after = partial(save_value, "sessionid", partial(extract_attribute, "/iq[@type='result']/commands:command[@node='configure']", "sessionid"))),
                     partial(send_stanza, "<iq type='set' id='id2' from='{jid_one}/{resource_one}' to='{irc_server_one}'>"
                                           "<command xmlns='http://jabber.org/protocol/commands' node='configure' sessionid='{sessionid}' action='next'>"
                                           "<x xmlns='jabber:x:data' type='submit'>"
                                           "<field var='ports'><value>6667</value></field>"
                                           "<field var='tls_ports'><value>6697</value><value>6670</value></field>"
                                           "<field var='throttle_limit'><value>9999</value></field>"
                                           "</x></command></iq>"),
                      partial(expect_stanza, "/iq[@type='result']/commands:command[@node='configure'][@status='completed']/commands:note[@type='info'][text()='Configuration successfully applied.']"),


                     partial(send_stanza,
                             "<presence from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}/{nick_one}' />"),
                     connection_sequence("irc.localhost", '{jid_one}/{resource_one}'),
                     partial(expect_stanza,
                             "/message/body[text()='Mode #foo [+nt] by {irc_host_one}']"),
                     partial(expect_stanza,
                             ("/presence[@to='{jid_one}/{resource_one}'][@from='#foo%{irc_server_one}/{nick_one}']/muc_user:x/muc_user:item[@affiliation='admin'][@role='moderator']",
                              "/presence/muc_user:x/muc_user:status[@code='110']")
                             ),
                     partial(expect_stanza, "/message[@from='#foo%{irc_server_one}'][@type='groupchat']/subject[not(text())]"),

                     # Send two channel messages
                     partial(send_stanza, "<message from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}' type='groupchat'><body>coucou</body></message>"),
                     partial(expect_stanza,
                             ("/message[@from='#foo%{irc_server_one}/{nick_one}'][@to='{jid_one}/{resource_one}'][@type='groupchat']/body[text()='coucou']",
                              "/message/stable_id:stanza-id[@by='#foo%{irc_server_one}'][@id]",)
                             ),

                     partial(send_stanza, "<message from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}' type='groupchat'><body>coucou 2</body></message>"),
                     # Record the current time
                     partial(expect_stanza, "/message[@from='#foo%{irc_server_one}/{nick_one}'][@to='{jid_one}/{resource_one}'][@type='groupchat']/body[text()='coucou 2']",
                             after = partial(save_current_timestamp_plus_delta, "first_timestamp", datetime.timedelta(seconds=1))),

                     # Wait two seconds before sending two new messages
                     partial(sleep_for, 2),
                     partial(send_stanza, "<message from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}' type='groupchat'><body>coucou 3</body></message>"),
                     partial(send_stanza, "<message from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}' type='groupchat'><body>coucou 4</body></message>"),
                     partial(expect_stanza, "/message[@type='groupchat']/body[text()='coucou 3']"),
                     partial(expect_stanza, "/message[@type='groupchat']/body[text()='coucou 4']",
                             after = partial(save_current_timestamp_plus_delta, "second_timestamp", datetime.timedelta(seconds=1))),

                     # join some other channel, to stay connected to the server even after leaving #foo
                     partial(send_stanza,
                             "<presence from='{jid_one}/{resource_one}' to='#DUMMY%{irc_server_one}/{nick_one}' />"),
                     partial(expect_stanza, "/message"),
                     partial(expect_stanza, "/presence/muc_user:x/muc_user:status[@code='110']"),
                     partial(expect_stanza, "/message/subject"),

                     # Leave #foo
                     partial(send_stanza, "<presence from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}' type='unavailable' />"),
                     partial(expect_stanza, "/presence[@type='unavailable']"),

                     partial(sleep_for, 0.2),

                     # Rejoin #foo, with some history limit
                     partial(send_stanza,
                             "<presence from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}/{nick_one}'><x xmlns='http://jabber.org/protocol/muc'><history maxchars='0'/></x></presence>"),
                     partial(expect_stanza, "/message"),
                     partial(expect_stanza, "/presence/muc_user:x/muc_user:status[@code='110']"),
                     partial(expect_stanza, "/message/subject"),

                     partial(send_stanza, "<presence from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}' type='unavailable' />"),
                     partial(expect_stanza, "/presence[@type='unavailable']"),

                     partial(sleep_for, 0.2),

                     # Rejoin #foo, with some history limit
                     partial(send_stanza,
                             "<presence from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}/{nick_one}'><x xmlns='http://jabber.org/protocol/muc'><history maxstanzas='3'/></x></presence>"),
                     partial(expect_stanza, "/message"),
                     partial(expect_stanza, "/presence/muc_user:x/muc_user:status[@code='110']"),
                     partial(expect_stanza,
                              "/message[@from='#foo%{irc_server_one}/{nick_one}'][@type='groupchat']/body[text()='coucou 2']"),
                     partial(expect_stanza,
                              "/message[@from='#foo%{irc_server_one}/{nick_one}'][@type='groupchat']/body[text()='coucou 3']"),
                     partial(expect_stanza,
                              "/message[@from='#foo%{irc_server_one}/{nick_one}'][@type='groupchat']/body[text()='coucou 4']"),
                     partial(expect_stanza, "/message/subject"),

                     partial(send_stanza, "<presence from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}' type='unavailable' />"),
                     partial(expect_stanza, "/presence[@type='unavailable']"),


                     # Rejoin #foo, with some history limit
                     partial(send_stanza,
                             "<presence from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}/{nick_one}'><x xmlns='http://jabber.org/protocol/muc'><history since='{first_timestamp}'/></x></presence>"),
                     partial(expect_stanza, "/message"),
                     partial(expect_stanza, "/presence/muc_user:x/muc_user:status[@code='110']"),
                     partial(expect_stanza,
                              "/message[@from='#foo%{irc_server_one}/{nick_one}'][@type='groupchat']/body[text()='coucou 3']"),
                     partial(expect_stanza,
                              "/message[@from='#foo%{irc_server_one}/{nick_one}'][@type='groupchat']/body[text()='coucou 4']"),
                     partial(expect_stanza, "/message/subject"),
                     partial(send_stanza, "<presence from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}' type='unavailable' />"),
                     partial(expect_stanza, "/presence[@type='unavailable']"),

                     # Rejoin #foo, with some history limit
                     partial(send_stanza,
                             "<presence from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}/{nick_one}'><x xmlns='http://jabber.org/protocol/muc'><history seconds='1'/></x></presence>"),
                     partial(expect_stanza, "/message"),
                     partial(expect_stanza, "/presence/muc_user:x/muc_user:status[@code='110']"),
                     partial(expect_stanza,
                              "/message[@from='#foo%{irc_server_one}/{nick_one}'][@type='groupchat']/body[text()='coucou 3']"),
                     partial(expect_stanza,
                              "/message[@from='#foo%{irc_server_one}/{nick_one}'][@type='groupchat']/body[text()='coucou 4']"),
                     partial(expect_stanza, "/message/subject"),
                     partial(send_stanza, "<presence from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}' type='unavailable' />"),
                     partial(expect_stanza, "/presence[@type='unavailable']"),

                     # Rejoin #foo, with some history limit
                     partial(send_stanza,
                             "<presence from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}/{nick_one}'><x xmlns='http://jabber.org/protocol/muc'><history seconds='5'/></x></presence>"),
                     partial(expect_stanza, "/message"),
                     partial(expect_stanza, "/presence/muc_user:x/muc_user:status[@code='110']"),
                     partial(expect_stanza,
                              "/message[@from='#foo%{irc_server_one}/{nick_one}'][@type='groupchat']/body[text()='coucou']"),                     partial(expect_stanza,
                              "/message[@from='#foo%{irc_server_one}/{nick_one}'][@type='groupchat']/body[text()='coucou 2']"),
                     partial(expect_stanza,
                              "/message[@from='#foo%{irc_server_one}/{nick_one}'][@type='groupchat']/body[text()='coucou 3']"),
                     partial(expect_stanza,
                              "/message[@from='#foo%{irc_server_one}/{nick_one}'][@type='groupchat']/body[text()='coucou 4']"),
                     partial(expect_stanza, "/message/subject"),
                     partial(send_stanza, "<presence from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}' type='unavailable' />"),
                     partial(expect_stanza, "/presence[@type='unavailable']"),

                 ]),
        Scenario("mam_on_fixed_server",
                 [
                     handshake_sequence(),

                     partial(send_stanza,
                             "<presence from='{jid_one}/{resource_one}' to='#foo@{biboumi_host}/{nick_one}' />"),
                     connection_sequence("irc.localhost", '{jid_one}/{resource_one}', fixed_irc_server=True),
                     partial(expect_stanza,
                             "/message/body[text()='Mode #foo [+nt] by {irc_host_one}']"),
                     partial(expect_stanza,
                             ("/presence[@to='{jid_one}/{resource_one}'][@from='#foo@{biboumi_host}/{nick_one}']/muc_user:x/muc_user:item[@affiliation='admin'][@role='moderator']",
                              "/presence/muc_user:x/muc_user:status[@code='110']")
                             ),
                     partial(expect_stanza, "/message[@from='#foo@{biboumi_host}'][@type='groupchat']/subject[not(text())]"),

                     partial(send_stanza, "<message from='{jid_one}/{resource_one}' to='#foo@{biboumi_host}' type='groupchat'><body>coucou</body></message>"),
                     partial(expect_stanza, "/message[@from='#foo@{biboumi_host}/{nick_one}'][@to='{jid_one}/{resource_one}'][@type='groupchat']/body[text()='coucou']"),

                     partial(send_stanza, "<message from='{jid_one}/{resource_one}' to='#foo@{biboumi_host}' type='groupchat'><body>coucou 2</body></message>"),
                     partial(expect_stanza, "/message[@from='#foo@{biboumi_host}/{nick_one}'][@to='{jid_one}/{resource_one}'][@type='groupchat']/body[text()='coucou 2']"),

                     # Retrieve the complete archive
                     partial(send_stanza, "<iq to='#foo@{biboumi_host}' from='{jid_one}/{resource_one}' type='set' id='id1'><query xmlns='urn:xmpp:mam:2' queryid='qid1' /></iq>"),

                     partial(expect_stanza,
                             ("/message/mam:result[@queryid='qid1']/forward:forwarded/delay:delay",
                              "/message/mam:result[@queryid='qid1']/forward:forwarded/client:message[@from='#foo@{biboumi_host}/{nick_one}'][@type='groupchat']/client:body[text()='coucou']")
                             ),
                     partial(expect_stanza,
                             ("/message/mam:result[@queryid='qid1']/forward:forwarded/delay:delay",
                              "/message/mam:result[@queryid='qid1']/forward:forwarded/client:message[@from='#foo@{biboumi_host}/{nick_one}'][@type='groupchat']/client:body[text()='coucou 2']")
                             ),
                 ], conf="fixed_server"),
         Scenario("default_mam_limit",
                 [
                     handshake_sequence(),

                     # Disable the throttling, otherwise it’s way too long
                     partial(send_stanza, "<iq type='set' id='id1' from='{jid_one}/{resource_one}' to='{irc_server_one}'><command xmlns='http://jabber.org/protocol/commands' node='configure' action='execute' /></iq>"),
                     partial(expect_stanza, "/iq[@type='result']",
                             after = partial(save_value, "sessionid", partial(extract_attribute, "/iq[@type='result']/commands:command[@node='configure']", "sessionid"))),
                     partial(send_stanza, "<iq type='set' id='id2' from='{jid_one}/{resource_one}' to='{irc_server_one}'>"
                                           "<command xmlns='http://jabber.org/protocol/commands' node='configure' sessionid='{sessionid}' action='next'>"
                                           "<x xmlns='jabber:x:data' type='submit'>"
                                           "<field var='ports'><value>6667</value></field>"
                                           "<field var='tls_ports'><value>6697</value><value>6670</value></field>"
                                           "<field var='throttle_limit'><value>9999</value></field>"
                                           "</x></command></iq>"),
                      partial(expect_stanza, "/iq[@type='result']/commands:command[@node='configure'][@status='completed']/commands:note[@type='info'][text()='Configuration successfully applied.']"),

                     partial(send_stanza,
                             "<presence from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}/{nick_one}' />"),
                     connection_sequence("irc.localhost", '{jid_one}/{resource_one}'),
                     partial(expect_stanza,
                             "/message/body[text()='Mode #foo [+nt] by {irc_host_one}']"),
                     partial(expect_stanza,
                            ("/presence[@to='{jid_one}/{resource_one}'][@from='#foo%{irc_server_one}/{nick_one}']/muc_user:x/muc_user:item[@affiliation='admin'][@role='moderator']",
                             "/presence/muc_user:x/muc_user:status[@code='110']")
                      ),
                     partial(expect_stanza, "/message[@from='#foo%{irc_server_one}'][@type='groupchat']/subject[not(text())]",
                             after = partial(save_value, "counter", lambda x: 0)),
                 ] + [
                     partial(send_stanza, "<message from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}' type='groupchat'><body>{counter}</body></message>"),
                     partial(expect_stanza,
                             "/message[@from='#foo%{irc_server_one}/{nick_one}'][@to='{jid_one}/{resource_one}'][@type='groupchat']/body[text()='{counter}']",
                             after = partial(save_value, "counter", lambda stanza: str(1 + int(extract_text("/message/body", stanza))))
                             ),
                 ] * 150 + [
                    # Retrieve the archive, without any restriction
                    partial(send_stanza, "<iq to='#foo%{irc_server_one}' from='{jid_one}/{resource_one}' type='set' id='id1'><query xmlns='urn:xmpp:mam:2' queryid='qid1' /></iq>"),
                    partial(expect_stanza,
                            ("/message/mam:result[@queryid='qid1']/forward:forwarded/delay:delay",
                            "/message/mam:result[@queryid='qid1']/forward:forwarded/client:message[@from='#foo%{irc_server_one}/{nick_one}'][@type='groupchat']/client:body[text()='0']")
                            ),
                 ] + [
                     # followed by 98 more messages
                    partial(expect_stanza,
                            ("/message/mam:result[@queryid='qid1']/forward:forwarded/delay:delay",
                            "/message/mam:result[@queryid='qid1']/forward:forwarded/client:message[@from='#foo%{irc_server_one}/{nick_one}'][@type='groupchat']/client:body")
                            ),
                  ] * 98 + [
                     # and finally the message "99"
                    partial(expect_stanza,
                            ("/message/mam:result[@queryid='qid1']/forward:forwarded/delay:delay",
                            "/message/mam:result[@queryid='qid1']/forward:forwarded/client:message[@from='#foo%{irc_server_one}/{nick_one}'][@type='groupchat']/client:body[text()='99']"),
                            after = partial(save_value, "last_uuid", partial(extract_attribute, "/message/mam:result", "id"))
                            ),
                     # And it should not be marked as complete
                    partial(expect_stanza,
                            ("/iq[@type='result'][@id='id1'][@from='#foo%{irc_server_one}'][@to='{jid_one}/{resource_one}']",
                             "/iq/mam:fin/rsm:set/rsm:last[text()='{last_uuid}']",
                             "!/iq//mam:fin[@complete='true']",
                             "/iq//mam:fin")),

                     # Retrieve the next page, using the “after” thingy
                    partial(send_stanza, "<iq to='#foo%{irc_server_one}' from='{jid_one}/{resource_one}' type='set' id='id2'><query xmlns='urn:xmpp:mam:2' queryid='qid2' ><set xmlns='http://jabber.org/protocol/rsm'><after>{last_uuid}</after></set></query></iq>"),

                    partial(expect_stanza,
                            ("/message/mam:result[@queryid='qid2']/forward:forwarded/delay:delay",
                            "/message/mam:result[@queryid='qid2']/forward:forwarded/client:message[@from='#foo%{irc_server_one}/{nick_one}'][@type='groupchat']/client:body[text()='100']")
                            ),
                  ] + 48 * [
                    partial(expect_stanza,
                            ("/message/mam:result[@queryid='qid2']/forward:forwarded/delay:delay",
                            "/message/mam:result[@queryid='qid2']/forward:forwarded/client:message[@from='#foo%{irc_server_one}/{nick_one}'][@type='groupchat']/client:body")
                            ),
                  ] + [
                    partial(expect_stanza,
                            ("/message/mam:result[@queryid='qid2']/forward:forwarded/delay:delay",
                            "/message/mam:result[@queryid='qid2']/forward:forwarded/client:message[@from='#foo%{irc_server_one}/{nick_one}'][@type='groupchat']/client:body[text()='149']"),
                            after = partial(save_value, "last_uuid", partial(extract_attribute, "/message/mam:result", "id"))
                            ),
                    partial(expect_stanza,
                            ("/iq[@type='result'][@id='id2'][@from='#foo%{irc_server_one}'][@to='{jid_one}/{resource_one}']",
                             "/iq/mam:fin/rsm:set/rsm:last[text()='{last_uuid}']",
                             "/iq//mam:fin[@complete='true']",
                             "/iq//mam:fin")),

                    # Send a request with a non-existing ID set as the “after” value.
                    partial(send_stanza, "<iq to='#foo%{irc_server_one}' from='{jid_one}/{resource_one}' type='set' id='id3'><query xmlns='urn:xmpp:mam:2' queryid='qid3' ><set xmlns='http://jabber.org/protocol/rsm'><after>DUMMY_ID</after></set></query></iq>"),
                    partial(expect_stanza, "/iq[@id='id3'][@type='error']/error[@type='cancel']/stanza:item-not-found"),

                     # Request the last page just BEFORE the last message in the archive
                     partial(send_stanza, "<iq to='#foo%{irc_server_one}' from='{jid_one}/{resource_one}' type='set' id='id3'><query xmlns='urn:xmpp:mam:2' queryid='qid3' ><set xmlns='http://jabber.org/protocol/rsm'><before></before></set></query></iq>"),

                    partial(expect_stanza,
                            ("/message/mam:result[@queryid='qid3']/forward:forwarded/delay:delay",
                            "/message/mam:result[@queryid='qid3']/forward:forwarded/client:message[@from='#foo%{irc_server_one}/{nick_one}'][@type='groupchat']/client:body[text()='50']")
                            ),
                  ] + 98 * [
                    partial(expect_stanza,
                            ("/message/mam:result[@queryid='qid3']/forward:forwarded/delay:delay",
                            "/message/mam:result[@queryid='qid3']/forward:forwarded/client:message[@from='#foo%{irc_server_one}/{nick_one}'][@type='groupchat']/client:body")
                            ),
                  ] + [
                    partial(expect_stanza,
                            ("/message/mam:result[@queryid='qid3']/forward:forwarded/delay:delay",
                            "/message/mam:result[@queryid='qid3']/forward:forwarded/client:message[@from='#foo%{irc_server_one}/{nick_one}'][@type='groupchat']/client:body[text()='149']"),
                            after = partial(save_value, "last_uuid", partial(extract_attribute, "/message/mam:result", "id"))
                            ),
                    partial(expect_stanza,
                            ("/iq[@type='result'][@id='id3'][@from='#foo%{irc_server_one}'][@to='{jid_one}/{resource_one}']",
                             "/iq/mam:fin/rsm:set/rsm:last[text()='{last_uuid}']",
                             "!/iq//mam:fin[@complete='true']",
                             "/iq//mam:fin")),

                     # Do the same thing, but with a limit value.
                     partial(send_stanza, "<iq to='#foo%{irc_server_one}' from='{jid_one}/{resource_one}' type='set' id='id4'><query xmlns='urn:xmpp:mam:2' queryid='qid4' ><set xmlns='http://jabber.org/protocol/rsm'><before>{last_uuid}</before><max>2</max></set></query></iq>"),
                    partial(expect_stanza,
                            ("/message/mam:result[@queryid='qid4']/forward:forwarded/delay:delay",
                            "/message/mam:result[@queryid='qid4']/forward:forwarded/client:message[@from='#foo%{irc_server_one}/{nick_one}'][@type='groupchat']/client:body[text()='147']")
                            ),
                    partial(expect_stanza,
                            ("/message/mam:result[@queryid='qid4']/forward:forwarded/delay:delay",
                            "/message/mam:result[@queryid='qid4']/forward:forwarded/client:message[@from='#foo%{irc_server_one}/{nick_one}'][@type='groupchat']/client:body[text()='148']"),
                            after = partial(save_value, "last_uuid", partial(extract_attribute, "/message/mam:result", "id"))
                            ),
                    partial(expect_stanza,
                            ("/iq[@type='result'][@id='id4'][@from='#foo%{irc_server_one}'][@to='{jid_one}/{resource_one}']",
                             "/iq/mam:fin/rsm:set/rsm:last[text()='{last_uuid}']",
                             "!/iq/mam:fin[@complete='true']",)),

                     # Test if everything is fine even with weird max value: 0
                     partial(send_stanza, "<iq to='#foo%{irc_server_one}' from='{jid_one}/{resource_one}' type='set' id='id5'><query xmlns='urn:xmpp:mam:2' queryid='qid5' ><set xmlns='http://jabber.org/protocol/rsm'><before></before><max>0</max></set></query></iq>"),

                     partial(expect_stanza,
                            ("/iq[@type='result'][@id='id5'][@from='#foo%{irc_server_one}'][@to='{jid_one}/{resource_one}']",
                             "!/iq/mam:fin[@complete='true']",)),
                  ]),
        Scenario("channel_history_on_fixed_server",
                 [
                     handshake_sequence(),
                     # First user join
                     partial(send_stanza,
                             "<presence from='{jid_one}/{resource_one}' to='#foo@{biboumi_host}/{nick_one}' />"),
                     connection_sequence("irc.localhost", '{jid_one}/{resource_one}', fixed_irc_server=True),
                     partial(expect_stanza,
                             "/message/body[text()='Mode #foo [+nt] by {irc_host_one}']"),
                     partial(expect_stanza,
                             ("/presence[@to='{jid_one}/{resource_one}'][@from='#foo@{biboumi_host}/{nick_one}']/muc_user:x/muc_user:item[@affiliation='admin'][@jid='~nick@localhost'][@role='moderator']",
                              "/presence/muc_user:x/muc_user:status[@code='110']")
                             ),
                     partial(expect_stanza, "/message[@from='#foo@{biboumi_host}'][@type='groupchat']/subject[not(text())]"),

                     # Send one channel message
                     partial(send_stanza, "<message from='{jid_one}/{resource_one}' to='#foo@{biboumi_host}' type='groupchat'><body>coucou</body></message>"),
                     partial(expect_stanza, "/message[@from='#foo@{biboumi_host}/{nick_one}'][@to='{jid_one}/{resource_one}'][@type='groupchat']/body[text()='coucou']"),

                     # Second user joins
                     partial(send_stanza,
                             "<presence from='{jid_one}/{resource_two}' to='#foo@{biboumi_host}/{nick_one}' />"),
                     partial(expect_stanza,
                             ("/presence[@to='{jid_one}/{resource_two}'][@from='#foo@{biboumi_host}/{nick_one}']/muc_user:x/muc_user:item[@affiliation='admin'][@jid='~nick@localhost'][@role='moderator']",
                              "/presence/muc_user:x/muc_user:status[@code='110']")
                             ),
                     # Receive the history message
                     partial(expect_stanza, ("/message[@from='#foo@{biboumi_host}/{nick_one}']/body[text()='coucou']",
                                             "/message/delay:delay[@from='#foo@{biboumi_host}']")),

                     partial(expect_stanza, "/message[@from='#foo@{biboumi_host}'][@type='groupchat']/subject[not(text())]"),
                 ], conf="fixed_server"),
    Scenario("channel_history",
             [
                 handshake_sequence(),
                 # First user join
                 partial(send_stanza,
                         "<presence from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}/{nick_one}' />"),
                 connection_sequence("irc.localhost", '{jid_one}/{resource_one}'),
                 partial(expect_stanza,
                         "/message/body[text()='Mode #foo [+nt] by {irc_host_one}']"),
                 partial(expect_stanza,
                         ("/presence[@to='{jid_one}/{resource_one}'][@from='#foo%{irc_server_one}/{nick_one}']/muc_user:x/muc_user:item[@affiliation='admin'][@jid='~nick@localhost'][@role='moderator']",
                          "/presence/muc_user:x/muc_user:status[@code='110']")
                         ),
                 partial(expect_stanza, "/message[@from='#foo%{irc_server_one}'][@type='groupchat']/subject[not(text())]"),

                 # Send one channel message
                 partial(send_stanza, "<message from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}' type='groupchat'><body>coucou</body></message>"),
                 partial(expect_stanza, "/message[@from='#foo%{irc_server_one}/{nick_one}'][@to='{jid_one}/{resource_one}'][@type='groupchat']/body[text()='coucou']"),

                 # Second user joins
                 partial(send_stanza,
                         "<presence from='{jid_one}/{resource_two}' to='#foo%{irc_server_one}/{nick_one}' />"),
                 partial(expect_stanza,
                         ("/presence[@to='{jid_one}/{resource_two}'][@from='#foo%{irc_server_one}/{nick_one}']/muc_user:x/muc_user:item[@affiliation='admin'][@jid='~nick@localhost'][@role='moderator']",
                          "/presence/muc_user:x/muc_user:status[@code='110']")
                         ),
                 # Receive the history message
                 partial(expect_stanza, ("/message[@from='#foo%{irc_server_one}/{nick_one}']/body[text()='coucou']",
                                         "/message/delay:delay[@from='#foo%{irc_server_one}']")),

                 partial(expect_stanza, "/message[@from='#foo%{irc_server_one}'][@type='groupchat']/subject[not(text())]"),
             ]),
        Scenario("simple_channel_list",
                 [
                     handshake_sequence(),

                     partial(send_stanza,
                             "<presence from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}/{nick_one}' />"),
                     connection_sequence("irc.localhost", '{jid_one}/{resource_one}'),
                     partial(expect_stanza,
                             "/message/body[text()='Mode #foo [+nt] by {irc_host_one}']"),
                     partial(expect_stanza,
                             ("/presence[@to='{jid_one}/{resource_one}'][@from='#foo%{irc_server_one}/{nick_one}']/muc_user:x/muc_user:item[@affiliation='admin'][@role='moderator']",
                             "/presence/muc_user:x/muc_user:status[@code='110']")
                             ),
                     partial(expect_stanza, "/message[@from='#foo%{irc_server_one}'][@type='groupchat']/subject[not(text())]"),

                     partial(send_stanza,
                             "<presence from='{jid_one}/{resource_one}' to='#bar%{irc_server_one}/{nick_one}' />"),
                     partial(expect_stanza,
                             "/message/body[text()='Mode #bar [+nt] by {irc_host_one}']"),
                     partial(expect_stanza, "/presence"),
                     partial(expect_stanza, "/message[@from='#bar%{irc_server_one}'][@type='groupchat']/subject[not(text())]"),

                     partial(send_stanza, "<iq from='{jid_one}/{resource_one}' id='id1' to='{irc_server_one}' type='get'><query xmlns='http://jabber.org/protocol/disco#items'/></iq>"),
                     partial(expect_stanza, (
                         "/iq[@type='result']/disco_items:query",
                         "/iq/disco_items:query/disco_items:item[@jid='#foo%{irc_server_one}']",
                         "/iq/disco_items:query/disco_items:item[@jid='#bar%{irc_server_one}']"
                     ))
                 ]),
        Scenario("channel_list_escaping",
                 [
                     handshake_sequence(),

                     partial(send_stanza,
                             "<presence from='{jid_one}/{resource_one}' to='#true\\2ffalse%{irc_server_one}/{nick_one}' />"),
                     connection_sequence("irc.localhost", '{jid_one}/{resource_one}'),
                     partial(expect_stanza,
                             "/message/body[text()='Mode #true/false [+nt] by {irc_host_one}']"),
                     partial(expect_stanza,
                             ("/presence[@to='{jid_one}/{resource_one}'][@from='#true\\2ffalse%{irc_server_one}/{nick_one}']/muc_user:x/muc_user:item[@affiliation='admin'][@role='moderator']",
                             "/presence/muc_user:x/muc_user:status[@code='110']")
                             ),
                     partial(expect_stanza, "/message[@from='#true\\2ffalse%{irc_server_one}'][@type='groupchat']/subject[not(text())]"),
                 ]),
        Scenario("channel_list_with_rsm",
                 [
                     handshake_sequence(),

                     partial(send_stanza,
                             "<presence from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}/{nick_one}' />"),
                     connection_sequence("irc.localhost", '{jid_one}/{resource_one}'),
                     partial(expect_stanza,
                             "/message/body[text()='Mode #foo [+nt] by {irc_host_one}']"),
                     partial(expect_stanza,
                             ("/presence[@to='{jid_one}/{resource_one}'][@from='#foo%{irc_server_one}/{nick_one}']/muc_user:x/muc_user:item[@affiliation='admin'][@role='moderator']",
                             "/presence/muc_user:x/muc_user:status[@code='110']")
                             ),
                     partial(expect_stanza, "/message[@from='#foo%{irc_server_one}'][@type='groupchat']/subject[not(text())]"),

                     partial(send_stanza,
                             "<presence from='{jid_one}/{resource_one}' to='#bar%{irc_server_one}/{nick_one}' />"),
                     partial(expect_stanza,
                             "/message/body[text()='Mode #bar [+nt] by {irc_host_one}']"),
                     partial(expect_stanza, "/presence"),
                     partial(expect_stanza, "/message[@from='#bar%{irc_server_one}'][@type='groupchat']/subject[not(text())]"),

                     partial(send_stanza,
                             "<presence from='{jid_one}/{resource_one}' to='#coucou%{irc_server_one}/{nick_one}' />"),
                     partial(expect_stanza,
                             "/message/body[text()='Mode #coucou [+nt] by {irc_host_one}']"),
                     partial(expect_stanza, "/presence"),
                     partial(expect_stanza, "/message[@from='#coucou%{irc_server_one}'][@type='groupchat']/subject[not(text())]"),

                     # Ask for 0 item
                     partial(send_stanza, "<iq from='{jid_one}/{resource_one}' id='id1' to='{irc_server_one}' type='get'><query xmlns='http://jabber.org/protocol/disco#items'><set xmlns='http://jabber.org/protocol/rsm'><max>0</max></set></query></iq>"),

                     # Get 0 item
                     partial(expect_stanza, (
                         "/iq[@type='result']/disco_items:query",
                         )),

                     # Ask for 2 (of 3) items We don’t have the count,
                     # because biboumi doesn’t have the complete list when
                     # it sends us the 2 items
                     partial(send_stanza, "<iq from='{jid_one}/{resource_one}' id='id1' to='{irc_server_one}' type='get'><query xmlns='http://jabber.org/protocol/disco#items'><set xmlns='http://jabber.org/protocol/rsm'><max>2</max></set></query></iq>"),
                     partial(expect_stanza, (
                         "/iq[@type='result']/disco_items:query",
                         "/iq/disco_items:query/disco_items:item[@jid='#bar%{irc_server_one}']",
                         "/iq/disco_items:query/disco_items:item[@jid='#coucou%{irc_server_one}']",
                         "/iq/disco_items:query/rsm:set/rsm:first[text()='#bar%{irc_server_one}'][@index='0']",
                         "/iq/disco_items:query/rsm:set/rsm:last[text()='#coucou%{irc_server_one}']"
                     )),

                     # Ask for 12 (of 3) items. We get the whole list, and thus we have the count included.
                     partial(send_stanza, "<iq from='{jid_one}/{resource_one}' id='id1' to='{irc_server_one}' type='get'><query xmlns='http://jabber.org/protocol/disco#items'><set xmlns='http://jabber.org/protocol/rsm'><max>12</max></set></query></iq>"),
                     partial(expect_stanza, (
                         "/iq[@type='result']/disco_items:query",
                         "/iq/disco_items:query/disco_items:item[@jid='#bar%{irc_server_one}']",
                         "/iq/disco_items:query/disco_items:item[@jid='#coucou%{irc_server_one}']",
                         "/iq/disco_items:query/disco_items:item[@jid='#foo%{irc_server_one}']",
                         "/iq/disco_items:query/rsm:set/rsm:first[text()='#bar%{irc_server_one}'][@index='0']",
                         "/iq/disco_items:query/rsm:set/rsm:last[text()='#foo%{irc_server_one}']",
                         "/iq/disco_items:query/rsm:set/rsm:count[text()='3']"
                     )),

                     # Ask for 1 item, AFTER the first item (so,
                     # the second). Since we don’t invalidate the cache
                     # with this request, we should have the count
                     # included.
                     partial(send_stanza, "<iq from='{jid_one}/{resource_one}' id='id1' to='{irc_server_one}' type='get'><query xmlns='http://jabber.org/protocol/disco#items'><set xmlns='http://jabber.org/protocol/rsm'><after>#bar%{irc_server_one}</after><max>1</max></set></query></iq>"),
                     partial(expect_stanza, (
                         "/iq[@type='result']/disco_items:query",
                         "/iq/disco_items:query/disco_items:item[@jid='#coucou%{irc_server_one}']",
                         "/iq/disco_items:query/rsm:set/rsm:first[text()='#coucou%{irc_server_one}'][@index='1']",
                         "/iq/disco_items:query/rsm:set/rsm:last[text()='#coucou%{irc_server_one}']",
                         "/iq/disco_items:query/rsm:set/rsm:count[text()='3']"
                     )),

                     # Ask for 1 item, AFTER the second item (so,
                     # the third).
                     partial(send_stanza, "<iq from='{jid_one}/{resource_one}' id='id1' to='{irc_server_one}' type='get'><query xmlns='http://jabber.org/protocol/disco#items'><set xmlns='http://jabber.org/protocol/rsm'><after>#coucou%{irc_server_one}</after><max>1</max></set></query></iq>"),
                     partial(expect_stanza, (
                         "/iq[@type='result']/disco_items:query",
                         "/iq/disco_items:query/disco_items:item[@jid='#foo%{irc_server_one}']",
                         "/iq/disco_items:query/rsm:set/rsm:first[text()='#foo%{irc_server_one}'][@index='2']",
                         "/iq/disco_items:query/rsm:set/rsm:last[text()='#foo%{irc_server_one}']",
                         "/iq/disco_items:query/rsm:set/rsm:count[text()='3']"
                     )),

                     # Ask for 1 item, AFTER the third item (so,
                     # the fourth). Since it doesn't exist, we get 0 item
                     partial(send_stanza, "<iq from='{jid_one}/{resource_one}' id='id1' to='{irc_server_one}' type='get'><query xmlns='http://jabber.org/protocol/disco#items'><set xmlns='http://jabber.org/protocol/rsm'><after>#foo%{irc_server_one}</after><max>1</max></set></query></iq>"),
                     partial(expect_stanza, (
                         "/iq[@type='result']/disco_items:query",
                         "/iq/disco_items:query/rsm:set/rsm:count[text()='3']"
                     )),
                 ]),
                Scenario("default_channel_list_limit",
                 [
                     handshake_sequence(),

                     # Disable the throttling, otherwise it’s way too long
                     partial(send_stanza, "<iq type='set' id='id1' from='{jid_one}/{resource_one}' to='{irc_server_one}'><command xmlns='http://jabber.org/protocol/commands' node='configure' action='execute' /></iq>"),
                     partial(expect_stanza, "/iq[@type='result']",
                             after = partial(save_value, "sessionid", partial(extract_attribute, "/iq[@type='result']/commands:command[@node='configure']", "sessionid"))),
                     partial(send_stanza, "<iq type='set' id='id2' from='{jid_one}/{resource_one}' to='{irc_server_one}'>"
                                           "<command xmlns='http://jabber.org/protocol/commands' node='configure' sessionid='{sessionid}' action='next'>"
                                           "<x xmlns='jabber:x:data' type='submit'>"
                                           "<field var='ports'><value>6667</value></field>"
                                           "<field var='tls_ports'><value>6697</value><value>6670</value></field>"
                                           "<field var='throttle_limit'><value>9999</value></field>"
                                           "</x></command></iq>"),
                      partial(expect_stanza, "/iq[@type='result']/commands:command[@node='configure'][@status='completed']/commands:note[@type='info'][text()='Configuration successfully applied.']"),

                     partial(send_stanza,
                             "<presence from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}/{nick_one}' />"),
                     connection_sequence("irc.localhost", '{jid_one}/{resource_one}'),
                     partial(expect_stanza, "/message"),
                     partial(expect_stanza, "/presence"),
                     partial(expect_stanza, "/message",
                             after = partial(save_value, "counter", lambda x: 0)),
                 ] + [
                     partial(send_stanza,
                             "<presence from='{jid_one}/{resource_one}' to='#{counter}%{irc_server_one}/{nick_one}' />"),
                     partial(expect_stanza, "/message"),
                     partial(expect_stanza, "/presence",
                             after = partial(save_value, "counter", lambda stanza: str(1 + int(chan_name_from_jid(extract_attribute("/presence", "from", stanza)))))),
                     partial(expect_stanza, "/message")
                 ] * 110 + [
                     partial(send_stanza, "<iq from='{jid_one}/{resource_one}' id='id1' to='{irc_server_one}' type='get'><query xmlns='http://jabber.org/protocol/disco#items'/></iq>"),
                     # charybdis sends the list in alphabetic order, so #foo is the last, and #99 is after #120
                     partial(expect_stanza, ("/iq/disco_items:query/disco_items:item[@jid='#0%{irc_server_one}']",
                                             "/iq/disco_items:query/disco_items:item[@jid='#1%{irc_server_one}']",
                                             "/iq/disco_items:query/disco_items:item[@jid='#109%{irc_server_one}']",
                                             "/iq/disco_items:query/disco_items:item[@jid='#9%{irc_server_one}']",
                                             "!/iq/disco_items:query/disco_items:item[@jid='#foo%{irc_server_one}']",
                                             "!/iq/disco_items:query/disco_items:item[@jid='#99%{irc_server_one}']",
                                             "!/iq/disco_items:query/disco_items:item[@jid='#90%{irc_server_one}']")),
                 ]),
                Scenario("complete_channel_list_with_pages_of_3",
                 [
                     handshake_sequence(),

                     partial(send_stanza,
                             "<presence from='{jid_one}/{resource_one}' to='#aaa%{irc_server_one}/{nick_one}' />"),
                     connection_sequence("irc.localhost", '{jid_one}/{resource_one}'),
                     partial(expect_stanza, "/message"),
                     partial(expect_stanza, "/presence"),
                     partial(expect_stanza, "/message"),

                     partial(send_stanza,
                             "<presence from='{jid_one}/{resource_one}' to='#bbb%{irc_server_one}/{nick_one}' />"),
                     partial(expect_stanza, "/message"),
                     partial(expect_stanza, "/presence"),
                     partial(expect_stanza, "/message"),

                     partial(send_stanza,
                             "<presence from='{jid_one}/{resource_one}' to='#ccc%{irc_server_one}/{nick_one}' />"),
                     partial(expect_stanza, "/message"),
                     partial(expect_stanza, "/presence"),
                     partial(expect_stanza, "/message"),

                     partial(send_stanza,
                             "<presence from='{jid_one}/{resource_one}' to='#ddd%{irc_server_one}/{nick_one}' />"),
                     partial(expect_stanza, "/message"),
                     partial(expect_stanza, "/presence"),
                     partial(expect_stanza, "/message"),

                     partial(send_stanza,
                             "<presence from='{jid_one}/{resource_one}' to='#eee%{irc_server_one}/{nick_one}' />"),
                     partial(expect_stanza, "/message"),
                     partial(expect_stanza, "/presence"),
                     partial(expect_stanza, "/message"),

                     partial(send_stanza,
                             "<presence from='{jid_one}/{resource_one}' to='#fff%{irc_server_one}/{nick_one}' />"),
                     partial(expect_stanza, "/message"),
                     partial(expect_stanza, "/presence"),
                     partial(expect_stanza, "/message"),

                     partial(send_stanza,
                             "<presence from='{jid_one}/{resource_one}' to='#ggg%{irc_server_one}/{nick_one}' />"),
                     partial(expect_stanza, "/message"),
                     partial(expect_stanza, "/presence"),
                     partial(expect_stanza, "/message"),

                     partial(send_stanza,
                             "<presence from='{jid_one}/{resource_one}' to='#hhh%{irc_server_one}/{nick_one}' />"),
                     partial(expect_stanza, "/message"),
                     partial(expect_stanza, "/presence"),
                     partial(expect_stanza, "/message"),

                     partial(send_stanza,
                             "<presence from='{jid_one}/{resource_one}' to='#iii%{irc_server_one}/{nick_one}' />"),
                     partial(expect_stanza, "/message"),
                     partial(expect_stanza, "/presence"),
                     partial(expect_stanza, "/message"),

                     partial(send_stanza,
                             "<presence from='{jid_one}/{resource_one}' to='#jjj%{irc_server_one}/{nick_one}' />"),
                     partial(expect_stanza, "/message"),
                     partial(expect_stanza, "/presence"),
                     partial(expect_stanza, "/message"),

                     partial(send_stanza, "<iq from='{jid_one}/{resource_one}' id='id' to='{irc_server_one}' type='get'><query xmlns='http://jabber.org/protocol/disco#items'><set xmlns='http://jabber.org/protocol/rsm'><max>3</max></set></query></iq>"),
                     partial(expect_stanza, (
                         "/iq[@type='result']/disco_items:query",
                         "/iq/disco_items:query/disco_items:item[@jid='#aaa%{irc_server_one}']",
                         "/iq/disco_items:query/disco_items:item[@jid='#bbb%{irc_server_one}']",
                         "/iq/disco_items:query/disco_items:item[@jid='#ccc%{irc_server_one}']",
                         "/iq/disco_items:query/rsm:set/rsm:first[text()='#aaa%{irc_server_one}'][@index='0']",
                         "/iq/disco_items:query/rsm:set/rsm:last[text()='#ccc%{irc_server_one}']"
                     )),

                     partial(send_stanza, "<iq from='{jid_one}/{resource_one}' id='id' to='{irc_server_one}' type='get'><query xmlns='http://jabber.org/protocol/disco#items'><set xmlns='http://jabber.org/protocol/rsm'><after>#ccc%{irc_server_one}</after><max>3</max></set></query></iq>"),
                     partial(expect_stanza, (
                         "/iq[@type='result']/disco_items:query",
                         "/iq/disco_items:query/disco_items:item[@jid='#ddd%{irc_server_one}']",
                         "/iq/disco_items:query/disco_items:item[@jid='#eee%{irc_server_one}']",
                         "/iq/disco_items:query/disco_items:item[@jid='#fff%{irc_server_one}']",
                         "/iq/disco_items:query/rsm:set/rsm:first[text()='#ddd%{irc_server_one}'][@index='3']",
                         "/iq/disco_items:query/rsm:set/rsm:last[text()='#fff%{irc_server_one}']"
                     )),

                     partial(send_stanza, "<iq from='{jid_one}/{resource_one}' id='id' to='{irc_server_one}' type='get'><query xmlns='http://jabber.org/protocol/disco#items'><set xmlns='http://jabber.org/protocol/rsm'><after>#fff%{irc_server_one}</after><max>3</max></set></query></iq>"),
                     partial(expect_stanza, (
                         "/iq[@type='result']/disco_items:query",
                         "/iq/disco_items:query/disco_items:item[@jid='#ggg%{irc_server_one}']",
                         "/iq/disco_items:query/disco_items:item[@jid='#hhh%{irc_server_one}']",
                         "/iq/disco_items:query/disco_items:item[@jid='#iii%{irc_server_one}']",
                         "/iq/disco_items:query/rsm:set/rsm:first[text()='#ggg%{irc_server_one}'][@index='6']",
                         "/iq/disco_items:query/rsm:set/rsm:last[text()='#iii%{irc_server_one}']"
                     )),

                     partial(send_stanza, "<iq from='{jid_one}/{resource_one}' id='id' to='{irc_server_one}' type='get'><query xmlns='http://jabber.org/protocol/disco#items'><set xmlns='http://jabber.org/protocol/rsm'><after>#iii%{irc_server_one}</after><max>3</max></set></query></iq>"),
                     partial(expect_stanza, (
                         "/iq[@type='result']/disco_items:query",
                         "/iq/disco_items:query/disco_items:item[@jid='#jjj%{irc_server_one}']",
                         "/iq/disco_items:query/rsm:set/rsm:first[text()='#jjj%{irc_server_one}'][@index='9']",
                         "/iq/disco_items:query/rsm:set/rsm:last[text()='#jjj%{irc_server_one}']",
                         "/iq/disco_items:query/rsm:set/rsm:count[text()='10']"
                     )),

                     partial(send_stanza, "<presence from='{jid_one}/{resource_one}' to='#aaa%{irc_server_one}/{nick_one}' type='unavailable' />"),
                     partial(send_stanza, "<presence from='{jid_one}/{resource_one}' to='#bbb%{irc_server_one}/{nick_one}' type='unavailable' />"),
                     partial(send_stanza, "<presence from='{jid_one}/{resource_one}' to='#ccc%{irc_server_one}/{nick_one}' type='unavailable' />"),
                     partial(send_stanza, "<presence from='{jid_one}/{resource_one}' to='#ddd%{irc_server_one}/{nick_one}' type='unavailable' />"),
                     partial(send_stanza, "<presence from='{jid_one}/{resource_one}' to='#eee%{irc_server_one}/{nick_one}' type='unavailable' />"),
                     partial(send_stanza, "<presence from='{jid_one}/{resource_one}' to='#fff%{irc_server_one}/{nick_one}' type='unavailable' />"),
                     partial(send_stanza, "<presence from='{jid_one}/{resource_one}' to='#ggg%{irc_server_one}/{nick_one}' type='unavailable' />"),
                     partial(send_stanza, "<presence from='{jid_one}/{resource_one}' to='#hhh%{irc_server_one}/{nick_one}' type='unavailable' />"),
                     partial(send_stanza, "<presence from='{jid_one}/{resource_one}' to='#iii%{irc_server_one}/{nick_one}' type='unavailable' />"),
                     partial(send_stanza, "<presence from='{jid_one}/{resource_one}' to='#jjj%{irc_server_one}/{nick_one}' type='unavailable' />"),
                     partial(expect_stanza, "/presence[@type='unavailable']"),
                     partial(expect_stanza, "/presence[@type='unavailable']"),
                     partial(expect_stanza, "/presence[@type='unavailable']"),
                     partial(expect_stanza, "/presence[@type='unavailable']"),
                     partial(expect_stanza, "/presence[@type='unavailable']"),
                     partial(expect_stanza, "/presence[@type='unavailable']"),
                     partial(expect_stanza, "/presence[@type='unavailable']"),
                     partial(expect_stanza, "/presence[@type='unavailable']"),
                     partial(expect_stanza, "/presence[@type='unavailable']"),
                     partial(expect_stanza, "/presence[@type='unavailable']")
                ]),
                Scenario("muc_traffic_info",
                [
                     handshake_sequence(),

                     partial(send_stanza,
                             "<iq from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}' id='1' type='get'><query xmlns='http://jabber.org/protocol/disco#info' node='http://jabber.org/protocol/muc#traffic'/></iq>"),
                     partial(expect_stanza, "/iq[@from='#foo%{irc_server_one}'][@to='{jid_one}/{resource_one}'][@type='result']/disco_info:query[@node='http://jabber.org/protocol/muc#traffic']"),
                ]),
                Scenario("muc_disco_info",
                [
                     handshake_sequence(),

                     partial(send_stanza,
                             "<iq from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}' id='1' type='get'><query xmlns='http://jabber.org/protocol/disco#info'/></iq>"),
                     partial(expect_stanza,
                             ("/iq[@from='#foo%{irc_server_one}'][@to='{jid_one}/{resource_one}'][@type='result']/disco_info:query",
                              "/iq[@type='result']/disco_info:query/disco_info:identity[@category='conference'][@type='irc'][@name='#foo on {irc_host_one}']",
                              "/iq/disco_info:query/disco_info:feature[@var='jabber:iq:version']",
                              "/iq/disco_info:query/disco_info:feature[@var='http://jabber.org/protocol/commands']",
                              "/iq/disco_info:query/disco_info:feature[@var='urn:xmpp:ping']",
                              "/iq/disco_info:query/disco_info:feature[@var='urn:xmpp:mam:2']",
                              "/iq/disco_info:query/disco_info:feature[@var='jabber:iq:version']",
                              "!/iq/disco_info:query/dataform:x/dataform:field[@var='muc#roominfo_occupants']"
                             )),

                    # Join the channel, and re-do the same query
                    partial(send_stanza,
                             "<presence from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}/{nick_one}' />"),
                     connection_sequence("irc.localhost", '{jid_one}/{resource_one}'),
                     partial(expect_stanza,
                             "/message/body[text()='Mode #foo [+nt] by {irc_host_one}']"),
                     partial(expect_stanza,
                             ("/presence[@to='{jid_one}/{resource_one}'][@from='#foo%{irc_server_one}/{nick_one}']/muc_user:x/muc_user:item[@affiliation='admin'][@role='moderator']",
                             "/presence/muc_user:x/muc_user:status[@code='110']")
                             ),
                     partial(expect_stanza, "/message[@from='#foo%{irc_server_one}'][@type='groupchat']/subject[not(text())]"),

                     partial(send_stanza,
                             "<iq from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}' id='2' type='get'><query xmlns='http://jabber.org/protocol/disco#info'/></iq>"),
                     partial(expect_stanza,
                             ("/iq[@from='#foo%{irc_server_one}'][@to='{jid_one}/{resource_one}'][@type='result']/disco_info:query",
                              "/iq/disco_info:query/dataform:x/dataform:field[@var='muc#roominfo_occupants']/dataform:value[text()='1']",
                              "/iq/disco_info:query/dataform:x/dataform:field[@var='FORM_TYPE'][@type='hidden']/dataform:value[text()='http://jabber.org/protocol/muc#roominfo']"
                             )),
                ]),
                Scenario("fixed_muc_disco_info",
                [
                     handshake_sequence(),

                     partial(send_stanza,
                             "<iq from='{jid_one}/{resource_one}' to='#foo@{biboumi_host}' id='1' type='get'><query xmlns='http://jabber.org/protocol/disco#info'/></iq>"),
                     partial(expect_stanza,
                             ("/iq[@from='#foo@{biboumi_host}'][@to='{jid_one}/{resource_one}'][@type='result']/disco_info:query",
                              "/iq[@type='result']/disco_info:query/disco_info:identity[@category='conference'][@type='irc'][@name='#foo on {irc_host_one}']",
                              "/iq/disco_info:query/disco_info:feature[@var='jabber:iq:version']",
                              "/iq/disco_info:query/disco_info:feature[@var='http://jabber.org/protocol/commands']",
                              "/iq/disco_info:query/disco_info:feature[@var='urn:xmpp:ping']",
                              "/iq/disco_info:query/disco_info:feature[@var='urn:xmpp:mam:2']",
                              "/iq/disco_info:query/disco_info:feature[@var='jabber:iq:version']",
                              )),
                ], conf='fixed_server'),
                Scenario("raw_message",
                [
                     handshake_sequence(),
                     partial(send_stanza, "<presence from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}/{nick_one}' />"),
                     connection_sequence("irc.localhost", '{jid_one}/{resource_one}'),
                     partial(expect_stanza, "/message"),
                     partial(expect_stanza, "/presence"),
                     partial(expect_stanza, "/message"),

                     partial(send_stanza, "<message from='{jid_one}/{resource_one}' to='{irc_server_one}' type='chat'><body>WHOIS {nick_one}</body></message>"),
                     partial(expect_stanza, "/message[@from='{irc_server_one}'][@type='chat']/body[text()='irc.localhost: {nick_one} ~{nick_one} localhost * {nick_one}']"),
                ]),
                Scenario("raw_message_fixed_irc_server",
                [
                     handshake_sequence(),
                     partial(send_stanza, "<presence from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}/{nick_one}' />"),
                     connection_sequence("irc.localhost", '{jid_one}/{resource_one}', fixed_irc_server=True),
                     partial(expect_stanza, "/message"),
                     partial(expect_stanza, "/presence"),
                     partial(expect_stanza, "/message"),

                     partial(send_stanza, "<message from='{jid_one}/{resource_one}' to='{biboumi_host}' type='chat'><body>WHOIS {nick_one}</body></message>"),
                     partial(expect_stanza, "/message[@from='{biboumi_host}'][@type='chat']/body[text()='irc.localhost: {nick_one} ~{nick_one} localhost * {nick_one}']"),
                ], conf='fixed_server'),
                Scenario("self_disco_info",
                [
                     handshake_sequence(),
                     partial(send_stanza, "<iq type='get' id='get1' from='{jid_one}/{resource_one}' to='{biboumi_host}'><query xmlns='http://jabber.org/protocol/disco#info'/></iq>"),
                     partial(expect_stanza,
                     ("/iq[@type='result']/disco_info:query/disco_info:identity[@category='conference'][@type='irc'][@name='Biboumi XMPP-IRC gateway']",
                      "/iq/disco_info:query/disco_info:feature[@var='jabber:iq:version']",
                      "/iq/disco_info:query/disco_info:feature[@var='http://jabber.org/protocol/commands']",
                      "/iq/disco_info:query/disco_info:feature[@var='urn:xmpp:ping']",
                      "/iq/disco_info:query/disco_info:feature[@var='urn:xmpp:mam:2']",
                      "/iq/disco_info:query/disco_info:feature[@var='jabber:iq:version']",
                      )),
                ]),
                Scenario("invite_other",
                [
                     handshake_sequence(),
                     partial(send_stanza, "<presence from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}/{nick_one}' />"),
                     connection_sequence("irc.localhost", '{jid_one}/{resource_one}'),
                     partial(expect_stanza, "/message"),
                     partial(expect_stanza, "/presence"),
                     partial(expect_stanza, "/message"),

                     partial(send_stanza, "<presence from='{jid_two}/{resource_two}' to='#bar%{irc_server_one}@{biboumi_host}/{nick_two}' />"),
                     connection_sequence("irc.localhost", '{jid_two}/{resource_two}'),
                     partial(expect_stanza, "/message"),
                     partial(expect_stanza, "/presence"),
                     partial(expect_stanza, "/message"),
                     partial(send_stanza, "<message from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}'><x xmlns='http://jabber.org/protocol/muc#user'><invite to='{nick_two}'/></x></message>"),
                     partial(expect_stanza, "/message/body[text()='{nick_two} has been invited to #foo']"),
                     partial(expect_stanza, "/message[@to='{jid_two}/{resource_two}'][@from='#foo%{irc_server_one}']/muc_user:x/muc_user:invite[@from='#foo%{irc_server_one}/{nick_one}']"),

                     partial(send_stanza, "<message from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}'><x xmlns='http://jabber.org/protocol/muc#user'><invite to='bertrand@example.com'/></x></message>"),
                     partial(expect_stanza, "/message[@to='bertrand@example.com'][@from='#foo%{irc_server_one}']/muc_user:x/muc_user:invite[@from='{jid_one}/{resource_one}']"),
                ]),
                Scenario("global_configure",
                [
                    handshake_sequence(),
                    partial(send_stanza, "<iq type='set' id='id1' from='{jid_one}/{resource_one}' to='{biboumi_host}'><command xmlns='http://jabber.org/protocol/commands' node='configure' action='execute' /></iq>"),
                    partial(expect_stanza, ("/iq[@type='result']/commands:command[@node='configure'][@sessionid][@status='executing']",
                                            "/iq/commands:command/dataform:x[@type='form']/dataform:title[text()='Configure some global default settings.']",
                                            "/iq/commands:command/dataform:x[@type='form']/dataform:instructions[text()='Edit the form, to configure your global settings for the component.']",
                                            "/iq/commands:command/dataform:x[@type='form']/dataform:field[@type='text-single'][@var='max_history_length']/dataform:value[text()='20']",
                                            "/iq/commands:command/dataform:x[@type='form']/dataform:field[@type='boolean'][@var='record_history']/dataform:value[text()='true']",
                                            "/iq/commands:command/dataform:x[@type='form']/dataform:field[@type='boolean'][@var='persistent']/dataform:value[text()='false']",
                                            "/iq/commands:command/commands:actions/commands:next",
                                            ),
                            after = partial(save_value, "sessionid", partial(extract_attribute, "/iq[@type='result']/commands:command[@node='configure']", "sessionid"))
                    ),
                    partial(send_stanza, "<iq type='set' id='id2' from='{jid_one}/{resource_one}' to='{biboumi_host}'><command xmlns='http://jabber.org/protocol/commands' node='configure' sessionid='{sessionid}' action='next'><x xmlns='jabber:x:data' type='submit'><field var='record_history'><value>0</value></field><field var='max_history_length'><value>42</value></field></x></command></iq>"),
                    partial(expect_stanza, "/iq[@type='result']/commands:command[@node='configure'][@status='completed']/commands:note[@type='info'][text()='Configuration successfully applied.']"),

                    partial(send_stanza, "<iq type='set' id='id3' from='{jid_one}/{resource_one}' to='{biboumi_host}'><command xmlns='http://jabber.org/protocol/commands' node='configure' action='execute' /></iq>"),
                    partial(expect_stanza, ("/iq[@type='result']/commands:command[@node='configure'][@sessionid][@status='executing']",
                                            "/iq/commands:command/dataform:x[@type='form']/dataform:title[text()='Configure some global default settings.']",
                                            "/iq/commands:command/dataform:x[@type='form']/dataform:instructions[text()='Edit the form, to configure your global settings for the component.']",
                                            "/iq/commands:command/dataform:x[@type='form']/dataform:field[@type='text-single'][@var='max_history_length']/dataform:value[text()='42']",
                                            "/iq/commands:command/dataform:x[@type='form']/dataform:field[@type='boolean'][@var='record_history']/dataform:value[text()='false']",
                                            "/iq/commands:command/dataform:x[@type='form']/dataform:field[@type='boolean'][@var='persistent']/dataform:value[text()='false']",
                                            "/iq/commands:command/commands:actions/commands:next",
                                            ),
                            after = partial(save_value, "sessionid", partial(extract_attribute, "/iq[@type='result']/commands:command[@node='configure']", "sessionid"))
                            ),
                    partial(send_stanza, "<iq type='set' id='id4' from='{jid_one}/{resource_one}' to='{biboumi_host}'><command xmlns='http://jabber.org/protocol/commands' action='cancel' node='configure' sessionid='{sessionid}' /></iq>"),
                    partial(expect_stanza, "/iq[@type='result']/commands:command[@node='configure'][@status='canceled']"),
                ]),
                Scenario("global_configure_fixed",
                [
                    handshake_sequence(),
                    partial(send_stanza, "<iq type='set' id='id1' from='{jid_one}/{resource_one}' to='{biboumi_host}'><command xmlns='http://jabber.org/protocol/commands' node='global-configure' action='execute' /></iq>"),
                    partial(expect_stanza, ("/iq[@type='result']/commands:command[@node='global-configure'][@sessionid][@status='executing']",
                                            "/iq/commands:command/dataform:x[@type='form']/dataform:title[text()='Configure some global default settings.']",
                                            "/iq/commands:command/dataform:x[@type='form']/dataform:instructions[text()='Edit the form, to configure your global settings for the component.']",
                                            "/iq/commands:command/dataform:x[@type='form']/dataform:field[@type='text-single'][@var='max_history_length']/dataform:value[text()='20']",
                                            "/iq/commands:command/dataform:x[@type='form']/dataform:field[@type='boolean'][@var='record_history']/dataform:value[text()='true']",
                                            "/iq/commands:command/dataform:x[@type='form']/dataform:field[@type='boolean'][@var='persistent']/dataform:value[text()='false']",
                                            "/iq/commands:command/commands:actions/commands:next",
                                            ),
                            after = partial(save_value, "sessionid", partial(extract_attribute, "/iq[@type='result']/commands:command[@node='global-configure']", "sessionid"))
                    ),
                    partial(send_stanza, "<iq type='set' id='id2' from='{jid_one}/{resource_one}' to='{biboumi_host}'><command xmlns='http://jabber.org/protocol/commands' node='global-configure' sessionid='{sessionid}' action='next'><x xmlns='jabber:x:data' type='submit'><field var='record_history'><value>0</value></field><field var='max_history_length'><value>42</value></field></x></command></iq>"),
                    partial(expect_stanza, "/iq[@type='result']/commands:command[@node='global-configure'][@status='completed']/commands:note[@type='info'][text()='Configuration successfully applied.']"),

                    partial(send_stanza, "<iq type='set' id='id3' from='{jid_one}/{resource_one}' to='{biboumi_host}'><command xmlns='http://jabber.org/protocol/commands' node='global-configure' action='execute' /></iq>"),
                    partial(expect_stanza, ("/iq[@type='result']/commands:command[@node='global-configure'][@sessionid][@status='executing']",
                                            "/iq/commands:command/dataform:x[@type='form']/dataform:title[text()='Configure some global default settings.']",
                                            "/iq/commands:command/dataform:x[@type='form']/dataform:instructions[text()='Edit the form, to configure your global settings for the component.']",
                                            "/iq/commands:command/dataform:x[@type='form']/dataform:field[@type='text-single'][@var='max_history_length']/dataform:value[text()='42']",
                                            "/iq/commands:command/dataform:x[@type='form']/dataform:field[@type='boolean'][@var='record_history']/dataform:value[text()='false']",
                                            "/iq/commands:command/dataform:x[@type='form']/dataform:field[@type='boolean'][@var='persistent']/dataform:value[text()='false']",
                                            "/iq/commands:command/commands:actions/commands:next",
                                            ),
                            after = partial(save_value, "sessionid", partial(extract_attribute, "/iq[@type='result']/commands:command[@node='global-configure']", "sessionid"))
                            ),
                    partial(send_stanza, "<iq type='set' id='id4' from='{jid_one}/{resource_one}' to='{biboumi_host}'><command xmlns='http://jabber.org/protocol/commands' action='cancel' node='global-configure' sessionid='{sessionid}' /></iq>"),
                    partial(expect_stanza, "/iq[@type='result']/commands:command[@node='global-configure'][@status='canceled']"),

                    partial(send_stanza, "<iq type='set' id='id1' from='{jid_one}/{resource_one}' to='{biboumi_host}'><command xmlns='http://jabber.org/protocol/commands' node='server-configure' action='execute' /></iq>"),
                    partial(expect_stanza, ("/iq[@type='result']/commands:command[@node='server-configure'][@sessionid][@status='executing']",))
                ], conf='fixed_server'),
         Scenario("global_configure_persistent_by_default",
                [
                    handshake_sequence(),
                    partial(send_stanza, "<iq type='set' id='id1' from='{jid_one}/{resource_one}' to='{biboumi_host}'><command xmlns='http://jabber.org/protocol/commands' node='configure' action='execute' /></iq>"),
                    partial(expect_stanza, ("/iq[@type='result']/commands:command[@node='configure'][@sessionid][@status='executing']",
                                            "/iq/commands:command/dataform:x[@type='form']/dataform:title[text()='Configure some global default settings.']",
                                            "/iq/commands:command/dataform:x[@type='form']/dataform:instructions[text()='Edit the form, to configure your global settings for the component.']",
                                            "/iq/commands:command/dataform:x[@type='form']/dataform:field[@type='text-single'][@var='max_history_length']/dataform:value[text()='20']",
                                            "/iq/commands:command/dataform:x[@type='form']/dataform:field[@type='boolean'][@var='record_history']/dataform:value[text()='true']",
                                            "/iq/commands:command/dataform:x[@type='form']/dataform:field[@type='boolean'][@var='persistent']/dataform:value[text()='true']",
                                            "/iq/commands:command/commands:actions/commands:next",
                                            ),
                    ),
                ],conf='persistent_by_default'),
        Scenario("irc_server_configure",
                 [
                     handshake_sequence(),
                     partial(send_stanza, "<iq type='set' id='id1' from='{jid_one}/{resource_one}' to='{irc_server_one}'><command xmlns='http://jabber.org/protocol/commands' node='configure' action='execute' /></iq>"),
                     partial(expect_stanza, ("/iq[@type='result']/commands:command[@node='configure'][@sessionid][@status='executing']",
                                             "/iq/commands:command/dataform:x[@type='form']/dataform:title[text()='Configure the IRC server irc.localhost']",
                                             "/iq/commands:command/dataform:x[@type='form']/dataform:instructions[text()='Edit the form, to configure the settings of the IRC server irc.localhost']",
                                             "/iq/commands:command/dataform:x[@type='form']/dataform:field[@type='text-multi'][@var='ports']/dataform:value[text()='6667']",
                                             "/iq/commands:command/dataform:x[@type='form']/dataform:field[@type='text-multi'][@var='tls_ports']/dataform:value[text()='6670']",
                                             "/iq/commands:command/dataform:x[@type='form']/dataform:field[@type='text-multi'][@var='tls_ports']/dataform:value[text()='6697']",
                                             "/iq/commands:command/dataform:x[@type='form']/dataform:field[@type='boolean'][@var='verify_cert']/dataform:value[text()='true']",
                                             "/iq/commands:command/dataform:x[@type='form']/dataform:field[@type='text-single'][@var='fingerprint']",
                                             "/iq/commands:command/dataform:x[@type='form']/dataform:field[@type='text-single'][@var='throttle_limit']",
                                             "/iq/commands:command/dataform:x[@type='form']/dataform:field[@type='text-private'][@var='pass']",
                                             "/iq/commands:command/dataform:x[@type='form']/dataform:field[@type='text-multi'][@var='after_connect_commands']",
                                             "/iq/commands:command/dataform:x[@type='form']/dataform:field[@type='text-single'][@var='nick']",
                                             "/iq/commands:command/dataform:x[@type='form']/dataform:field[@type='text-single'][@var='username']",
                                             "/iq/commands:command/dataform:x[@type='form']/dataform:field[@type='text-single'][@var='realname']",
                                             "/iq/commands:command/dataform:x[@type='form']/dataform:field[@type='text-single'][@var='encoding_in']",
                                             "/iq/commands:command/dataform:x[@type='form']/dataform:field[@type='text-single'][@var='encoding_out']",
                                             "/iq/commands:command/commands:actions/commands:next",
                                             ),
                             after = partial(save_value, "sessionid", partial(extract_attribute, "/iq[@type='result']/commands:command[@node='configure']", "sessionid"))
                             ),
                     partial(send_stanza, "<iq type='set' id='id2' from='{jid_one}/{resource_one}' to='{irc_server_one}'>"
                                          "<command xmlns='http://jabber.org/protocol/commands' node='configure' sessionid='{sessionid}' action='next'>"
                                          "<x xmlns='jabber:x:data' type='submit'>"
                                          "<field var='ports' />"
                                          "<field var='tls_ports'><value>6697</value><value>6698</value></field>"
                                          "<field var='verify_cert'><value>1</value></field>"
                                          "<field var='fingerprint'><value>12:12:12</value></field>"
                                          "<field var='pass'><value>coucou</value></field>"
                                          "<field var='after_connect_commands'><value>first command</value><value>second command</value></field>"
                                          "<field var='nick'><value>my_nickname</value></field>"
                                          "<field var='username'><value>username</value></field>"
                                          "<field var='throttle_limit'><value>42</value></field>"
                                          "<field var='realname'><value>realname</value></field>"
                                          "<field var='encoding_out'><value>UTF-8</value></field>"
                                          "<field var='encoding_in'><value>latin-1</value></field>"
                                          "</x></command></iq>"),
                     partial(expect_stanza, "/iq[@type='result']/commands:command[@node='configure'][@status='completed']/commands:note[@type='info'][text()='Configuration successfully applied.']"),

                     partial(send_stanza, "<iq type='set' id='id3' from='{jid_one}/{resource_one}' to='{irc_server_one}'><command xmlns='http://jabber.org/protocol/commands' node='configure' action='execute' /></iq>"),
                     partial(expect_stanza, ("/iq[@type='result']/commands:command[@node='configure'][@sessionid][@status='executing']",
                                             "/iq/commands:command/dataform:x[@type='form']/dataform:title[text()='Configure the IRC server irc.localhost']",
                                             "/iq/commands:command/dataform:x[@type='form']/dataform:instructions[text()='Edit the form, to configure the settings of the IRC server irc.localhost']",
                                             "/iq/commands:command/dataform:x[@type='form']/dataform:field[@type='text-multi']",
                                             "/iq/commands:command/dataform:x[@type='form']/dataform:field[@type='text-multi'][@var='tls_ports']/dataform:value[text()='6697']",
                                             "/iq/commands:command/dataform:x[@type='form']/dataform:field[@type='text-multi'][@var='tls_ports']/dataform:value[text()='6698']",
                                             "/iq/commands:command/dataform:x[@type='form']/dataform:field[@type='boolean'][@var='verify_cert']/dataform:value[text()='true']",
                                             "/iq/commands:command/dataform:x[@type='form']/dataform:field[@type='text-single'][@var='fingerprint']/dataform:value[text()='12:12:12']",
                                             "/iq/commands:command/dataform:x[@type='form']/dataform:field[@type='text-private'][@var='pass']/dataform:value[text()='coucou']",
                                             "/iq/commands:command/dataform:x[@type='form']/dataform:field[@type='text-single'][@var='nick']/dataform:value[text()='my_nickname']",
                                             "/iq/commands:command/dataform:x[@type='form']/dataform:field[@type='text-multi'][@var='after_connect_commands']/dataform:value[text()='first command']",
                                             "/iq/commands:command/dataform:x[@type='form']/dataform:field[@type='text-multi'][@var='after_connect_commands']/dataform:value[text()='second command']",
                                             "/iq/commands:command/dataform:x[@type='form']/dataform:field[@type='text-single'][@var='username']/dataform:value[text()='username']",
                                             "/iq/commands:command/dataform:x[@type='form']/dataform:field[@type='text-single'][@var='realname']/dataform:value[text()='realname']",
                                             "/iq/commands:command/dataform:x[@type='form']/dataform:field[@type='text-single'][@var='throttle_limit']/dataform:value[text()='42']",
                                             "/iq/commands:command/dataform:x[@type='form']/dataform:field[@type='text-single'][@var='encoding_in']/dataform:value[text()='latin-1']",
                                             "/iq/commands:command/dataform:x[@type='form']/dataform:field[@type='text-single'][@var='encoding_out']/dataform:value[text()='UTF-8']",
                                             "/iq/commands:command/commands:actions/commands:next",
                                             ),
                             after = partial(save_value, "sessionid", partial(extract_attribute, "/iq[@type='result']/commands:command[@node='configure']", "sessionid"))
                             ),
                     partial(send_stanza, "<iq type='set' id='id4' from='{jid_one}/{resource_one}' to='{irc_server_one}'><command xmlns='http://jabber.org/protocol/commands' action='cancel' node='configure' sessionid='{sessionid}' /></iq>"),
                     partial(expect_stanza, "/iq[@type='result']/commands:command[@node='configure'][@status='canceled']"),

                     # Same thing, but try to empty some values
                     partial(send_stanza, "<iq type='set' id='id1' from='{jid_one}/{resource_one}' to='{irc_server_one}'><command xmlns='http://jabber.org/protocol/commands' node='configure' action='execute' /></iq>"),
                     partial(expect_stanza, "/iq[@type='result']",
                             after = partial(save_value, "sessionid", partial(extract_attribute, "/iq[@type='result']/commands:command[@node='configure']", "sessionid"))
                             ),
                     partial(send_stanza, "<iq type='set' id='id2' from='{jid_one}/{resource_one}' to='{irc_server_one}'>"
                                          "<command xmlns='http://jabber.org/protocol/commands' node='configure' sessionid='{sessionid}' action='next'>"
                                          "<x xmlns='jabber:x:data' type='submit'>"
                                          "<field var='pass'><value></value></field>"
                                          "<field var='after_connect_commands'></field>"
                                          "<field var='username'><value></value></field>"
                                          "<field var='realname'><value></value></field>"
                                          "<field var='throttle_limit'><value></value></field>"
                                          "<field var='encoding_out'><value></value></field>"
                                          "<field var='encoding_in'><value></value></field>"
                                          "</x></command></iq>"),
                     partial(expect_stanza, "/iq[@type='result']/commands:command[@node='configure'][@status='completed']/commands:note[@type='info'][text()='Configuration successfully applied.']"),

                     partial(send_stanza, "<iq type='set' id='id3' from='{jid_one}/{resource_one}' to='{irc_server_one}'><command xmlns='http://jabber.org/protocol/commands' node='configure' action='execute' /></iq>"),
                     partial(expect_stanza, ("/iq[@type='result']/commands:command[@node='configure'][@sessionid][@status='executing']",
                                             "/iq/commands:command/dataform:x[@type='form']/dataform:title[text()='Configure the IRC server irc.localhost']",
                                             "/iq/commands:command/dataform:x[@type='form']/dataform:instructions[text()='Edit the form, to configure the settings of the IRC server irc.localhost']",
                                             "!/iq/commands:command/dataform:x/dataform:field[@var='tls_ports']/dataform:value",
                                             "!/iq/commands:command/dataform:x[@type='form']/dataform:field[@var='pass']/dataform:value",
                                             "!/iq/commands:command/dataform:x[@type='form']/dataform:field[@var='after_connect_commands']/dataform:value",
                                             "!/iq/commands:command/dataform:x[@type='form']/dataform:field[@var='username']/dataform:value",
                                             "!/iq/commands:command/dataform:x[@type='form']/dataform:field[@var='realname']/dataform:value",
                                             "!/iq/commands:command/dataform:x[@type='form']/dataform:field[@var='encoding_in']/dataform:value",
                                             "!/iq/commands:command/dataform:x[@type='form']/dataform:field[@var='encoding_out']/dataform:value",
                                             "/iq/commands:command/commands:actions/commands:next",
                                             "/iq/commands:command/dataform:x[@type='form']/dataform:field[@type='text-single'][@var='throttle_limit']/dataform:value[text()='-1']",  # An invalid value sets this field to -1, aka disabled
                                             ),
                             after = partial(save_value, "sessionid", partial(extract_attribute, "/iq[@type='result']/commands:command[@node='configure']", "sessionid"))
                             ),
                     partial(send_stanza, "<iq type='set' id='id4' from='{jid_one}/{resource_one}' to='{irc_server_one}'><command xmlns='http://jabber.org/protocol/commands' action='cancel' node='configure' sessionid='{sessionid}' /></iq>"),
                     partial(expect_stanza, "/iq[@type='result']/commands:command[@node='configure'][@status='canceled']"),

                 ]),
        Scenario("irc_channel_configure",
                 [
                     handshake_sequence(),
                     partial(send_stanza, "<iq type='set' id='id1' from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}'><command xmlns='http://jabber.org/protocol/commands' node='configure' action='execute'><dummy/></command></iq>"),
                     partial(expect_stanza, ("/iq[@type='result']/commands:command[@node='configure'][@sessionid][@status='executing']",
                                             "/iq/commands:command/dataform:x[@type='form']/dataform:field[@type='text-single'][@var='encoding_in']",
                                             "/iq/commands:command/dataform:x[@type='form']/dataform:field[@type='text-single'][@var='encoding_out']",
                                             "/iq/commands:command/dataform:x[@type='form']/dataform:field[@type='list-single'][@var='record_history']/dataform:value[text()='unset']",
                                             "!/iq/commands:command/commands:dummy",
                                             ),
                                             after = partial(save_value, "sessionid", partial(extract_attribute, "/iq[@type='result']/commands:command[@node='configure']", "sessionid"))
                             ),
                     partial(send_stanza, "<iq type='set' id='id2' from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}'>"
                                          "<command xmlns='http://jabber.org/protocol/commands' node='configure' sessionid='{sessionid}' action='next'>"
                                          "<x xmlns='jabber:x:data' type='submit'>"
                                          "<field var='ports' />"
                                          "<field var='encoding_out'><value>UTF-8</value></field>"
                                          "<field var='encoding_in'><value>latin-1</value></field>"
                                          "<field var='record_history'><value>true</value></field>"
                                          "</x></command></iq>"),
                     partial(expect_stanza, "/iq[@type='result']/commands:command[@node='configure'][@status='completed']/commands:note[@type='info'][text()='Configuration successfully applied.']"),

                     partial(send_stanza, "<iq type='set' id='id3' from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}'><command xmlns='http://jabber.org/protocol/commands' node='configure' action='execute' /></iq>"),
                     partial(expect_stanza, ("/iq[@type='result']/commands:command[@node='configure'][@sessionid][@status='executing']",
                                             "/iq/commands:command/dataform:x[@type='form']/dataform:title[text()='Configure the IRC channel #foo on server irc.localhost']",
                                             "/iq/commands:command/dataform:x[@type='form']/dataform:field[@type='text-single'][@var='encoding_in']/dataform:value[text()='latin-1']",
                                             "/iq/commands:command/dataform:x[@type='form']/dataform:field[@type='text-single'][@var='encoding_out']/dataform:value[text()='UTF-8']",
                                             "/iq/commands:command/dataform:x[@type='form']/dataform:field[@type='list-single'][@var='record_history']/dataform:value[text()='true']",
                                             "/iq/commands:command/commands:actions/commands:next",
                                             ),
                             after = partial(save_value, "sessionid", partial(extract_attribute, "/iq[@type='result']/commands:command[@node='configure']", "sessionid"))
                             ),
                     partial(send_stanza, "<iq type='set' id='id4' from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}'><command xmlns='http://jabber.org/protocol/commands' action='cancel' node='configure' sessionid='{sessionid}' /></iq>"),
                     partial(expect_stanza, "/iq[@type='result']/commands:command[@node='configure'][@status='canceled']"),
                 ]),
        Scenario("irc_channel_configure_xep0045",
                 [
                     handshake_sequence(),
                     partial(send_stanza, "<iq type='get' id='id1' from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}'><query xmlns='http://jabber.org/protocol/muc#owner'/></iq>"),
                     partial(expect_stanza, ("/iq[@type='result']/muc_owner:query",
                                             "/iq/muc_owner:query/dataform:x[@type='form']/dataform:field[@type='text-single'][@var='encoding_in']",
                                             "/iq/muc_owner:query/dataform:x[@type='form']/dataform:field[@type='text-single'][@var='encoding_out']",
                                             ),
                             ),
                     partial(send_stanza, "<iq type='set' id='id2' from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}'>"
                                          "<query xmlns='http://jabber.org/protocol/muc#owner'>"
                                          "<x xmlns='jabber:x:data' type='submit'>"
                                          "<field var='ports' />"
                                          "<field var='encoding_out'><value>UTF-8</value></field>"
                                          "<field var='encoding_in'><value>latin-1</value></field>"
                                          "</x></query></iq>"),
                     partial(expect_stanza, "/iq[@type='result']"),
                     partial(send_stanza, "<iq type='set' id='id3' from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}'><query xmlns='http://jabber.org/protocol/muc#owner'>    <x xmlns='jabber:x:data' type='cancel'/></query></iq>"),
                     partial(expect_stanza, "/iq[@type='result']"),
                 ]),
        Scenario("irc_channel_configure_fixed",
                 [
                     handshake_sequence(),
                     partial(send_stanza, "<iq type='set' id='id1' from='{jid_one}/{resource_one}' to='#foo@{biboumi_host}'><command xmlns='http://jabber.org/protocol/commands' node='configure' action='execute' /></iq>"),
                     partial(expect_stanza, ("/iq[@type='result']/commands:command[@node='configure'][@sessionid][@status='executing']",
                                             "/iq/commands:command/dataform:x[@type='form']/dataform:field[@type='text-single'][@var='encoding_in']",
                                             "/iq/commands:command/dataform:x[@type='form']/dataform:field[@type='text-single'][@var='encoding_out']",
                                             ),
                             after = partial(save_value, "sessionid", partial(extract_attribute, "/iq[@type='result']/commands:command[@node='configure']", "sessionid"))
                             ),
                     partial(send_stanza, "<iq type='set' id='id2' from='{jid_one}/{resource_one}' to='#foo@{biboumi_host}'>"
                                          "<command xmlns='http://jabber.org/protocol/commands' node='configure' sessionid='{sessionid}' action='next'>"
                                          "<x xmlns='jabber:x:data' type='submit'>"
                                          "<field var='ports' />"
                                          "<field var='encoding_out'><value>UTF-8</value></field>"
                                          "<field var='encoding_in'><value>latin-1</value></field>"
                                          "</x></command></iq>"),
                     partial(expect_stanza, "/iq[@type='result']/commands:command[@node='configure'][@status='completed']/commands:note[@type='info'][text()='Configuration successfully applied.']"),

                     partial(send_stanza, "<iq type='set' id='id3' from='{jid_one}/{resource_one}' to='#foo@{biboumi_host}'><command xmlns='http://jabber.org/protocol/commands' node='configure' action='execute' /></iq>"),
                     partial(expect_stanza, ("/iq[@type='result']/commands:command[@node='configure'][@sessionid][@status='executing']",
                                             "/iq/commands:command/dataform:x[@type='form']/dataform:title[text()='Configure the IRC channel #foo on server irc.localhost']",
                                             "/iq/commands:command/dataform:x[@type='form']/dataform:field[@type='text-single'][@var='encoding_in']/dataform:value[text()='latin-1']",
                                             "/iq/commands:command/dataform:x[@type='form']/dataform:field[@type='text-single'][@var='encoding_out']/dataform:value[text()='UTF-8']",
                                             "/iq/commands:command/commands:actions/commands:next",
                                             ),
                             after = partial(save_value, "sessionid", partial(extract_attribute, "/iq[@type='result']/commands:command[@node='configure']", "sessionid"))
                             ),
                     partial(send_stanza, "<iq type='set' id='id4' from='{jid_one}/{resource_one}' to='#foo@{biboumi_host}'><command xmlns='http://jabber.org/protocol/commands' action='cancel' node='configure' sessionid='{sessionid}' /></iq>"),
                     partial(expect_stanza, "/iq[@type='result']/commands:command[@node='configure'][@status='canceled']"),
                 ], conf='fixed_server'),
         Scenario("irc_tls_connection",
                  [
                     handshake_sequence(),
                     # First, use an adhoc command to configure how we connect to the irc server, configure
                     # only one TLS port, and disable the cert verification.
                     partial(send_stanza, "<iq type='set' id='id1' from='{jid_one}/{resource_one}' to='{irc_server_one}'><command xmlns='http://jabber.org/protocol/commands' node='configure' action='execute' /></iq>"),
                     partial(expect_stanza, "/iq[@type='result']",
                             after = partial(save_value, "sessionid", partial(extract_attribute, "/iq[@type='result']/commands:command[@node='configure']", "sessionid"))),
                     partial(send_stanza, "<iq type='set' id='id2' from='{jid_one}/{resource_one}' to='{irc_server_one}'>"
                                           "<command xmlns='http://jabber.org/protocol/commands' node='configure' sessionid='{sessionid}' action='next'>"
                                           "<x xmlns='jabber:x:data' type='submit'>"
                                           "<field var='ports' />"
                                           "<field var='tls_ports'><value>7778</value></field>"
                                           "<field var='verify_cert'><value>0</value></field>"
                                           "<field var='nick'><value>my_special_nickname</value></field>"
                                           "</x></command></iq>"),
                      partial(expect_stanza, "/iq[@type='result']/commands:command[@node='configure'][@status='completed']/commands:note[@type='info'][text()='Configuration successfully applied.']"),

                      partial(send_stanza,
                              "<presence from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}/{nick_one}' />"),
                      connection_tls_sequence("irc.localhost", '{jid_one}/{resource_one}'),
                      partial(expect_stanza,
                              "/message/body[text()='Mode #foo [+nt] by {irc_host_one}']"),
                      partial(expect_stanza,
                              ("/presence[@to='{jid_one}/{resource_one}'][@from='#foo%{irc_server_one}/my_special_nickname']/muc_user:x/muc_user:item[@affiliation='admin'][@role='moderator']",
                               "/presence/muc_user:x/muc_user:status[@code='110']")
                              ),
                      partial(expect_stanza, "/message[@from='#foo%{irc_server_one}'][@type='groupchat']/subject[not(text())]"),
                  ]),
         Scenario("get_irc_connection_info",
                 [
                     handshake_sequence(),

                     partial(send_stanza, "<iq type='set' id='command1' from='{jid_one}/{resource_one}' to='{irc_server_one}'><command xmlns='http://jabber.org/protocol/commands' node='get-irc-connection-info' action='execute' /></iq>"),
                     partial(expect_stanza, "/iq/commands:command/commands:note[text()='You are not connected to the IRC server irc.localhost']"),

                     partial(send_stanza,
                             "<presence from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}/{nick_one}' />"),
                     connection_sequence("irc.localhost", '{jid_one}/{resource_one}'),
                     partial(expect_stanza, "/message"),
                     partial(expect_stanza, "/presence"),
                     partial(expect_stanza, "/message"),

                     partial(send_stanza, "<iq type='set' id='command2' from='{jid_one}/{resource_one}' to='{irc_server_one}'><command xmlns='http://jabber.org/protocol/commands' node='get-irc-connection-info' action='execute' /></iq>"),
                     partial(expect_stanza, r"/iq/commands:command/commands:note[re:test(text(), 'Connected to IRC server irc.localhost on port 6667 since \d\d\d\d-\d\d-\d\d \d\d:\d\d:\d\d \(\d+ seconds ago\)\.\n#foo from 1 resource: {resource_one}.*')]"),
                 ]),
         Scenario("get_irc_connection_info_fixed",
                 [
                     handshake_sequence(),

                     partial(send_stanza, "<iq type='set' id='command1' from='{jid_one}/{resource_one}' to='{biboumi_host}'><command xmlns='http://jabber.org/protocol/commands' node='get-irc-connection-info' action='execute' /></iq>"),
                     partial(expect_stanza, "/iq/commands:command/commands:note[text()='You are not connected to the IRC server irc.localhost']"),

                     partial(send_stanza,
                             "<presence from='{jid_one}/{resource_one}' to='#foo@{biboumi_host}/{nick_one}' />"),
                     connection_sequence("irc.localhost", '{jid_one}/{resource_one}', fixed_irc_server=True),
                     partial(expect_stanza, "/message"),
                     partial(expect_stanza, "/presence"),
                     partial(expect_stanza, "/message"),

                     partial(send_stanza, "<iq type='set' id='command2' from='{jid_one}/{resource_one}' to='{biboumi_host}'><command xmlns='http://jabber.org/protocol/commands' node='get-irc-connection-info' action='execute' /></iq>"),
                     partial(expect_stanza, r"/iq/commands:command/commands:note[re:test(text(), 'Connected to IRC server irc.localhost on port 6667 since \d\d\d\d-\d\d-\d\d \d\d:\d\d:\d\d \(\d+ seconds ago\)\.\n#foo from 1 resource: {resource_one}.*')]"),
                 ], conf='fixed_server'),
        Scenario("irc_server_presence_subscription",
                  [
                      handshake_sequence(),
                      partial(send_stanza, "<presence type='subscribe' from='{jid_one}/{resource_one}' to='{irc_server_one}' id='sub1' />"),
                      partial(expect_stanza, "/presence[@to='{jid_one}'][@from='{irc_server_one}'][@type='subscribed']")
                  ]),
        Scenario("fixed_irc_server_presence_subscription",
                  [
                      handshake_sequence(),
                      partial(send_stanza, "<presence type='subscribe' from='{jid_one}/{resource_one}' to='{biboumi_host}' id='sub1' />"),
                      partial(expect_stanza, "/presence[@to='{jid_one}'][@from='{biboumi_host}'][@type='subscribed']")
                  ], conf='fixed_server'),
        Scenario("leave_unjoined_chan",
                  [
                      handshake_sequence(),
                      partial(send_stanza, "<presence from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}/{nick_one}' />"),
                      connection_sequence("irc.localhost", '{jid_one}/{resource_one}'),
                      partial(expect_stanza, "/message"),
                      partial(expect_stanza, "/presence"),
                      partial(expect_stanza, "/message"),

                      partial(send_stanza, "<presence from='{jid_two}/{resource_two}' to='#foo%{irc_server_one}/{nick_one}' />"),
                      connection_begin_sequence("irc.localhost", '{jid_two}/{resource_two}'),

                      partial(expect_stanza, "/message[@to='{jid_two}/{resource_two}'][@type='chat']/body[text()='irc.localhost: {nick_one}: Nickname is already in use.']"),
                      partial(expect_stanza, "/presence[@type='error']/error[@type='cancel'][@code='409']/stanza:conflict"),
                      partial(send_stanza, "<presence from='{jid_two}/{resource_two}' to='#foo%{irc_server_one}/{nick_one}' type='unavailable' />")
         ]),
        Scenario("basic_subscribe_unsubscribe",
                 [
                     handshake_sequence(),

                     # Mutual subscription exchange
                     partial(send_stanza, "<presence from='{jid_one}' to='{biboumi_host}' type='subscribe' id='subid1' />"),
                     partial(expect_stanza, "/presence[@type='subscribed'][@id='subid1']"),

                     # Get the current presence of the biboumi gateway
                     partial(expect_stanza, "/presence"),

                     partial(expect_stanza, "/presence[@type='subscribe']"),
                     partial(send_stanza, "<presence from='{jid_one}' to='{biboumi_host}' type='subscribed' />"),


                     # Unsubscribe
                     partial(send_stanza, "<presence from='{jid_one}' to='{biboumi_host}' type='unsubscribe' id='unsubid1' />"),
                     partial(expect_stanza, "/presence[@type='unavailable']"),
                     partial(expect_stanza, "/presence[@type='unsubscribed']"),
                     partial(expect_stanza, "/presence[@type='unsubscribe']"),
                     partial(send_stanza, "<presence from='{jid_one}' to='{biboumi_host}' type='unavailable' />"),
                     partial(send_stanza, "<presence from='{jid_one}' to='{biboumi_host}' type='unsubscribed' />"),
                 ]),
        Scenario("resource_is_removed_from_server_when_last_chan_is_left",
                 [
                     # Join the channel
                     handshake_sequence(),
                     partial(send_stanza,
                             "<presence from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}/{nick_one}' />"),
                     connection_sequence("irc.localhost", '{jid_one}/{resource_one}'),
                     partial(expect_stanza,
                             "/message/body[text()='Mode #foo [+nt] by {irc_host_one}']"),
                     partial(expect_stanza,
                             ("/presence[@to='{jid_one}/{resource_one}'][@from='#foo%{irc_server_one}/{nick_one}']/muc_user:x/muc_user:item[@affiliation='admin'][@role='moderator']",
                              "/presence/muc_user:x/muc_user:status[@code='110']")
                             ),
                     partial(expect_stanza, "/message[@from='#foo%{irc_server_one}'][@type='groupchat'][@to='{jid_one}/{resource_one}']/subject[not(text())]"),

                     # Make it persistent
                     partial(send_stanza, "<iq from='{jid_one}/{resource_one}' id='conf1' to='#foo%{irc_server_one}' type='get'><query xmlns='http://jabber.org/protocol/muc#owner'/></iq>"),
                     partial(expect_stanza, "/iq[@type='result']/muc_owner:query/dataform:x/dataform:field[@var='persistent'][@type='boolean']/dataform:value[text()='false']"),
                     partial(send_stanza, "<iq from='{jid_one}/{resource_one}' id='conf2' to='#foo%{irc_server_one}' type='set'><query xmlns='http://jabber.org/protocol/muc#owner'><x type='submit' xmlns='jabber:x:data'><field var='persistent' xmlns='jabber:x:data'><value>true</value></field></x></query></iq>"),
                     partial(expect_stanza, "/iq[@type='result']"),

                     partial(send_stanza, "<presence from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}/{nick_one}' type='unavailable' />"),
                     partial(expect_stanza, "/presence[@type='unavailable'][@from='#foo%{irc_server_one}/{nick_one}']"),

                     # Join the same channel, with the same JID, but a different resource
                     partial(send_stanza,
                             "<presence from='{jid_one}/{resource_two}' to='#foo%{irc_server_one}/{nick_one}' />"),
                     partial(expect_stanza,
                             ("/presence[@to='{jid_one}/{resource_two}'][@from='#foo%{irc_server_one}/{nick_one}']/muc_user:x/muc_user:item[@affiliation='admin'][@role='moderator']",
                              "/presence/muc_user:x/muc_user:status[@code='110']")
                             ),
                     partial(expect_stanza, "/message[@from='#foo%{irc_server_one}'][@type='groupchat'][@to='{jid_one}/{resource_two}']/subject[not(text())]"),

                     # Join some other channel with someone else
                     partial(send_stanza,
                             "<presence from='{jid_two}/{resource_one}' to='#bar%{irc_server_one}/{nick_two}' />"),
                     connection_sequence("irc.localhost", '{jid_two}/{resource_one}'),
                     partial(expect_stanza,
                             "/message/body[text()='Mode #bar [+nt] by {irc_host_one}']"),
                     partial(expect_stanza,
                             ("/presence[@to='{jid_two}/{resource_one}'][@from='#bar%{irc_server_one}/{nick_two}']/muc_user:x/muc_user:item[@affiliation='admin'][@role='moderator']",
                              "/presence/muc_user:x/muc_user:status[@code='110']")
                             ),
                     partial(expect_stanza, "/message[@from='#bar%{irc_server_one}'][@type='groupchat'][@to='{jid_two}/{resource_one}']/subject[not(text())]"),

                     # Send two messages from the second user to the first one
                     partial(send_stanza, "<message from='{jid_two}/{resource_one}' to='{lower_nick_one}%{irc_server_one}' type='chat'><body>kikoo</body></message>"),
                     partial(send_stanza, "<message from='{jid_two}/{resource_one}' to='{lower_nick_one}%{irc_server_one}' type='chat'><body>second kikoo</body></message>"),

                     # We must receive each message only once, no duplicate
                     partial(expect_stanza, "/message/body[text()='kikoo']"),
                     partial(expect_stanza, "/message/body[text()='second kikoo']"),
                  ]
                 ),
        Scenario("irc_server_presence_in_roster",
                 [
                     handshake_sequence(),

                     # Mutual subscription exchange
                     partial(send_stanza, "<presence from='{jid_one}' to='{irc_server_one}' type='subscribe' id='subid1' />"),
                     partial(expect_stanza, "/presence[@type='subscribed'][@id='subid1']"),

                     partial(expect_stanza, "/presence[@type='subscribe']"),
                     partial(send_stanza, "<presence from='{jid_one}' to='{irc_server_one}' type='subscribed' />"),

                     # Join a channel on that server
                     partial(send_stanza,
                             "<presence from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}/{nick_one}' />"),

                     # We must receive the IRC server presence, in the connection sequence
                     connection_sequence("irc.localhost", '{jid_one}/{resource_one}', expected_irc_presence=True),
                     partial(expect_stanza,
                             "/message/body[text()='Mode #foo [+nt] by {irc_host_one}']"),
                     partial(expect_stanza,
                             ("/presence[@to='{jid_one}/{resource_one}'][@from='#foo%{irc_server_one}/{nick_one}']/muc_user:x/muc_user:item[@affiliation='admin'][@role='moderator']",
                             "/presence/muc_user:x/muc_user:status[@code='110']")
                             ),
                     partial(expect_stanza, "/message[@from='#foo%{irc_server_one}'][@type='groupchat']/subject[not(text())]"),

                     # Leave the channel, and thus the IRC server
                     partial(send_stanza, "<presence type='unavailable' from='{jid_one}/{resource_one}' to='#foo%{irc_server_one}/{nick_one}' />"),
                     partial(expect_stanza, "/presence[@type='unavailable'][@from='#foo%{irc_server_one}/{nick_one}']"),
                     partial(expect_stanza, "/presence[@from='{irc_server_one}'][@to='{jid_one}'][@type='unavailable']"),
                 ])
    )

    failures = 0

    scenar_list = sys.argv[1:]
    irc_output = open("irc_output.txt", "w")
    irc = IrcServerRunner()
    print("Starting irc server…")
    asyncio.get_event_loop().run_until_complete(irc.start())
    while True:
        res = asyncio.get_event_loop().run_until_complete(irc.process.stderr.readline())
        irc_output.write(res.decode())
        if not res:
            print("IRC server failed to start, see irc_output.txt for more details. Exiting…")
            sys.exit(1)
        if b"now running in foreground mode" in res:
            break
    print("irc server started.")
    checks = len([s for s in scenarios if s.name in scenar_list]) if scenar_list else len(scenarios)
    print("Running %s checks for biboumi." % checks)

    for s in scenarios:
        if scenar_list and s.name not in scenar_list:
            continue
        test = BiboumiTest(s)
        if not test.run():
            print("You can check the files slixmpp_%s_output.txt and biboumi_%s_output.txt to help you debug." %
                  (s.name, s.name))
            failures += 1

    print("Waiting for irc server to exit…")
    irc.stop()
    asyncio.get_event_loop().run_until_complete(irc.wait())

    if failures:
        print("%d test%s failed, please fix %s." % (failures, 's' if failures > 1 else '',
                                                    'them' if failures > 1 else 'it'))
        sys.exit(1)
    else:
        print("All tests passed successfully")
