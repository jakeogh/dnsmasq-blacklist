#!/usr/bin/env python3
# tab-width:4
# pylint: disable=missing-docstring

# PUBLIC DOMAIN
# http://github.com/jkeogh/dnsgate
#
# "psl domain" is "Public Second Level Domain"
# extracted using https://publicsuffix.org/
# essentially this is the first level at which
# the public could register domains for a given TLD
__version__ = "0.0.1"

import click
import copy
import time
import glob
import hashlib
import sys
import os
import ast
import shutil
import requests
import tldextract
import pprint
import configparser
from shutil import copyfileobj
import logging
import string

class logmaker():
    def __init__(self, output_format, name, level):
        self.logger = logging.getLogger(name)
        self.logger_ch = logging.StreamHandler()
        self.formatter = logging.Formatter(output_format)
        self.logger_ch.setFormatter(self.formatter)
        self.logger.addHandler(self.logger_ch)
        self.logger.setLevel(level)

LOG = {
    'CRITICAL': logging.CRITICAL, # 50
    'ERROR':    logging.ERROR,    # 40
    'WARNING':  logging.WARNING,  # 30  # python default level
    'INFO':     logging.INFO,     # 20
    'DEBUG':    logging.DEBUG     # 10
    }

FORMAT = "%(levelname)-5s %(lineno)4s %(filename)-18s:%(funcName)-13s : %(message)s"
QUIET_FORMAT = "%(message)s"
logger_quiet = logmaker(output_format=QUIET_FORMAT, name="logging_quiet",
    level=LOG['INFO'])

def set_verbose(ctx, param, verbose=False):
    if verbose:
        logger_quiet.logger.setLevel(LOG['DEBUG'])
    else:
        logger_quiet.logger.setLevel(LOG['INFO'] + 1)

DEFAULT_OUTPUT_FILE_NAME = 'generated_blacklist'
CONFIG_DIRECTORY         = '/etc/dnsgate'
CONFIG_FILE              = CONFIG_DIRECTORY + '/config'
CUSTOM_BLACKLIST         = CONFIG_DIRECTORY + '/blacklist'
CUSTOM_WHITELIST         = CONFIG_DIRECTORY + '/whitelist'
DEFAULT_OUTPUT_FILE      = CONFIG_DIRECTORY + '/' + DEFAULT_OUTPUT_FILE_NAME
CACHE_DIRECTORY          = CONFIG_DIRECTORY + '/cache'
TLDEXTRACT_CACHE         = CACHE_DIRECTORY + '/tldextract_cache'

DNSMASQ_CONFIG_INCLUDE_DIRECTORY = '/etc/dnsmasq.d'
DNSMASQ_CONFIG_FILE      = '/etc/dnsmasq.conf'
DNSMASQ_CONFIG_SYMLINK   = DNSMASQ_CONFIG_INCLUDE_DIRECTORY + '/' + \
    DEFAULT_OUTPUT_FILE_NAME
DEFAULT_REMOTE_BLACKLISTS = [
    'http://winhelp2002.mvps.org/hosts.txt',
    'http://someonewhocares.org/hosts/hosts']
ALL_REMOTE_BLACKLISTS = [
    'http://winhelp2002.mvps.org/hosts.txt',
    'http://someonewhocares.org/hosts/hosts',
    'https://adaway.org/hosts.txt',
    'https://raw.githubusercontent.com/StevenBlack/hosts/master/data/StevenBlack/hosts',
    'http://www.malwaredomainlist.com/hostslist/hosts.txt',
    'http://pgl.yoyo.org/adservers/serverlist.php?hostformat=hosts;showintro=0']

CACHE_EXPIRE = 3600 * 24 * 2 # 48 hours
TLD_EXTRACT = tldextract.TLDExtract(cache_file=TLDEXTRACT_CACHE)

def eprint(*args, level, **kwargs):
    if level == LOG['INFO']:
        logger_quiet.logger.info(*args, **kwargs)
    elif level >= LOG['WARNING']:
        logger_quiet.logger.warning(*args, **kwargs)

def make_custom_blacklist_header(path):
    output_file_header = '#' * 64 + '''
# dnsgate custom blacklist
# User-defined blacklisted domains go here.
# Rules defined here override conflicting rules in ''' + CUSTOM_WHITELIST + '''
#
# Examples:
# google.com    # blocks *.google.com
# biz           # blocks the TLD biz completely (*.biz)'''
    return output_file_header

def make_custom_whitelist_header(path):
    output_file_header = '#' * 64 + '''
# dnsgate custom whitelist
# User-defined whitelisted domains go here.
# Usually this is only needed if block_at_psl is enabled in ''' + CONFIG_FILE + '''
# Rules here ARE OVERRIDDEN by any conflicting rules in ''' + CUSTOM_BLACKLIST + '''
#
# Examples:
# s3.amazonaws.com    # allows s3.amazonaws.com
#                           if something.s3.amazonaws.com is in
#                           "dnsmasq generate [sources]" it's still blocked
#                           unless explicitely whitelisted here.
# lwn.net             # allows lwn.net
#                           as above, explicitely blacklisted subdomains are blocked'''
    return output_file_header

def make_output_file_header(config_dict):
    configuration_string = '\n'.join(['#    ' + str(key) + ': ' +
        str(config_dict[key]) for key in sorted(config_dict.keys())])
    output_file_header = '#' * 64 + '''\n#
# AUTOMATICALLY GENERATED BY dnsgate\n#
# CHANGES WILL BE LOST ON THE NEXT RUN.\n#
# EDIT ''' + CUSTOM_BLACKLIST + ' or ' + \
        CUSTOM_WHITELIST + ' instead.\n#\n' + \
        '# Generated by:\n# ' + ' '.join(sys.argv) + \
        '\n#' + '\n# Configuration:\n' + configuration_string + \
        '\n#\n' + '#' * 64 + '\n\n'
    return output_file_header.encode('utf8')

def contains_whitespace(s):
    return True in [c in s for c in string.whitespace]

class Dnsgate_Config():
    def __init__(self, mode=False, dnsmasq_config_file=None, backup=False,
            no_restart_dnsmasq=False, block_at_psl=False, dest_ip=None,
            sources=None):
        self.mode = mode
        self.no_restart_dnsmasq = no_restart_dnsmasq
        self.backup = backup
        self.dnsmasq_config_file = dnsmasq_config_file
        self.block_at_psl = block_at_psl
        self.dest_ip = dest_ip
        self.sources = sources

# todo, check return code, run disable() and try again if the service fails
def restart_dnsmasq_service():
    if os.path.lexists('/etc/init.d/dnsmasq'):
        os.system('/etc/init.d/dnsmasq restart 1>&2')
    else:
        os.system('systemctl restart dnsmasq 1>&2')  # untested
    return True

def hash_str(string):
    assert isinstance(string, str)
    assert len(string) > 0
    return hashlib.sha1(string.encode('utf-8')).hexdigest()

def remove_comments_from_bytes(line):
    assert isinstance(line, bytes)
    uncommented_line = b''
    for char in line:
        char = bytes([char])
        if char != b'#':
            uncommented_line += char
        else:
            break
    return uncommented_line

def comment_out_line_in_file(fh, line_to_match):
    '''
    add a # to the beginning of all instances of line_to_match
    iff there is not already a # preceding line_to_match and
        line_to_match is the only thing on the line
            except possibly a preceeding # and/or whitespace

    if line_to_match is found and all instances are commented return True
    if line_to_match is found and all instances already commented return True
    if line_to_match is not found return False
    '''
    with open(fh.name, 'r') as rfh:
        lines = rfh.read().splitlines()
    newlines = []
    commented = False
    for line in lines:
        if line_to_match in line:
            line_stripped = line.strip()
            if line_stripped.startswith('#'):
                newlines.append(line)
                commented = True
                continue
            else:
                if line_stripped == line:
                    newlines.append('#' + line)
                    commented = True
                    continue
                else:
                    newlines.append(line)
                    continue
        else:
            newlines.append(line)
    if lines != newlines:
        fh.write('\n'.join(newlines) + '\n')
        return True
    elif commented:
        return True
    return False

def uncomment_line_in_file(fh, line_to_match):
    '''
    remove # from the beginning of all instances of line_to_match
    iff there is already a # preceding line_to_match and
        line_to_match is the only thing on the line
            except possibly a preceeding # and/or whitespace

    if line_to_match is found and all instances uncommented return True
    if line_to_match is found and all instances already uncommented return True
    if line_to_match is not found return False
    '''
    with open(fh.name, 'r') as rfh:
        lines = rfh.read().splitlines()
    newlines = []
    uncommented = False
    for line in lines:
        if line_to_match in line:
            line_stripped = line.strip()
            if line_stripped.startswith('#'):
                newlines.append(line[1:])
                uncommented = True
                continue
            else:
                if line_stripped == line:
                    newlines.append(line)
                    uncommented = True
                    continue
                else:
                    newlines.append(line)
                    continue
        else:
            newlines.append(line)

    if lines != newlines:
        fh.write('\n'.join(newlines) + '\n')
        return True
    if uncommented:
        return True
    return False

def group_by_tld(domains):
    eprint('Sorting domains by their subdomain and grouping by TLD.',
        level=LOG['INFO'])
    sorted_output = []
    reversed_domains = []
    for domain in domains:
        rev_domain = domain.split(b'.')
        rev_domain.reverse()
        reversed_domains.append(rev_domain)
    reversed_domains.sort() # sorting a list of lists by the tld
    for rev_domain in reversed_domains:
        rev_domain.reverse()
        sorted_output.append(b'.'.join(rev_domain))
    return sorted_output

def extract_psl_domain(domain):
    dom = TLD_EXTRACT(domain.decode('utf-8'))
    dom = dom.domain + '.' + dom.suffix
    return dom.encode('utf-8')

def strip_to_psl(domains):
    '''This causes ad-serving domains to be blocked at their root domain.
    Otherwise the subdomain can be changed until the --url lists are updated.
    It does not make sense to use this flag if you are generating a /etc/hosts
    format file since the effect would be to block google.com and not
    *.google.com.'''
    eprint('Removing subdomains on %d domains.', len(domains),
        level=LOG['INFO'])
    domains_stripped = set()
    for line in domains:
        line = extract_psl_domain(line)
        domains_stripped.add(line)
    return domains_stripped

def write_unique_line(line, file_to_write):
    '''
    Write line to file_to_write iff line not in file_to_write.
    '''
    try:
        with open(file_to_write, 'r+') as fh:
            if line not in fh:
                fh.write(line)
    except FileNotFoundError:
        with open(file_to_write, 'a') as fh:
            fh.write(line)

def backup_file_if_exists(file_to_backup):
    timestamp = str(time.time())
    dest_file = file_to_backup.name + '.bak.' + timestamp
    try:
        with open(file_to_backup.name, 'r') as sf:
            with open(dest_file, 'x') as df:
                copyfileobj(sf, df)
    except FileNotFoundError:
        pass    # skip backup if file does not exist

def validate_domain_list(domains):
    eprint('Validating %d domains.', len(domains), level=LOG['DEBUG'])
    valid_domains = set([])
    for hostname in domains:
        try:
            hostname = hostname.decode('utf-8')
            hostname = hostname.encode('idna').decode('ascii')
            valid_domains.add(hostname.encode('utf-8'))
        except Exception as e:
            eprint("WARNING: %s is not a valud domain. Skipping", hostname,
                level=LOG['WARNING'])
    return valid_domains

def generate_dnsmasq_config_file_line():
    return 'conf-dir=' + DNSMASQ_CONFIG_INCLUDE_DIRECTORY

def dnsmasq_install_help(dnsmasq_config_file, output_file=DEFAULT_OUTPUT_FILE):
    dnsmasq_config_file_line = generate_dnsmasq_config_file_line()
    print('    $ cp -vi ' + dnsmasq_config_file + ' ' + dnsmasq_config_file +
        '.bak.' + str(time.time()), file=sys.stderr)
    print('    $ grep ' + dnsmasq_config_file_line + ' ' + dnsmasq_config_file +
        '|| { echo ' + dnsmasq_config_file_line + ' >> dnsmasq_config_file ; }',
        file=sys.stderr)
    print('    $ /etc/init.d/dnsmasq restart', file=sys.stderr)

def hosts_install_help(output_file=DEFAULT_OUTPUT_FILE):
    print('    $ mv -vi /etc/hosts /etc/hosts.default', file=sys.stderr)
    print('    $ cat /etc/hosts.default ' + output_file + ' > /etc/hosts',
        file=sys.stderr)

def append_to_local_rule_file(rule_file, idn):
    eprint("attempting to append %s to %s", idn, rule_file, level=LOG['INFO'])
    hostname = idn.encode('idna').decode('ascii')
    eprint("appending hostname: %s to %s", hostname, rule_file, level=LOG['DEBUG'])
    line = hostname + '\n'
    write_unique_line(line, rule_file)

def extract_domain_set_from_dnsgate_format_file(dnsgate_file):
    domains = set([])
    dnsgate_file = os.path.abspath(dnsgate_file)
    dnsgate_file_bytes = read_file_bytes(dnsgate_file)
    lines = dnsgate_file_bytes.splitlines()
    for line in lines:
        line = line.strip()
        line = remove_comments_from_bytes(line)
        # ignore leading/trailing .
        line = b'.'.join(list(filter(None, line.split(b'.'))))
        if len(line) > 0:
            domains.add(line)
    return set(domains)

def read_file_bytes(path):
    with open(path, 'rb') as fh:
        file_bytes = fh.read()
    return file_bytes

def extract_domain_set_from_hosts_format_url_or_cached_copy(url, no_cache=False,
        cache_expire=CACHE_EXPIRE):
    unexpired_copy = get_newest_unexpired_cached_url_copy(url=url,
        cache_expire=cache_expire)
    if unexpired_copy:
        eprint("Using cached copy: %s", unexpired_copy, level=LOG['INFO'])
        unexpired_copy_bytes = read_file_bytes(unexpired_copy)
        assert isinstance(unexpired_copy_bytes, bytes)
        return extract_domain_set_from_hosts_format_bytes(unexpired_copy_bytes)
    else:
        return extract_domain_set_from_hosts_format_url(url, no_cache)

def generate_cache_file_name(url):
    url_hash = hash_str(url)
    file_name = CACHE_DIRECTORY + '/' + url_hash + '_hosts'
    return file_name

def get_newest_unexpired_cached_url_copy(url, cache_expire=CACHE_EXPIRE):
    newest_copy = get_matching_cached_file(url)
    if newest_copy:
        newest_copy_timestamp = os.stat(newest_copy).st_mtime
        expiration_timestamp = int(newest_copy_timestamp) + int(cache_expire)
        if expiration_timestamp > time.time():
            return newest_copy
        else:
            os.rename(newest_copy, newest_copy + '.expired')
            return False
    return False

def get_matching_cached_file(url):
    name = generate_cache_file_name(url)
    matching_cached_file = glob.glob(name)
    if matching_cached_file:
        return matching_cached_file[0]
    else:
        return False

def read_url_bytes(url, no_cache=False):
    eprint("GET: %s", url, level=LOG['DEBUG'])
    user_agent = 'Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:24.0) Gecko/20100101 Firefox/24.0'
    try:
        raw_url_bytes = requests.get(url, headers={'User-Agent': user_agent},
            allow_redirects=True, stream=False, timeout=15.500).content
    except Exception as e:
        eprint(e, level=LOG['WARNING'])
        return False
    if not no_cache:
        cache_index_file = CACHE_DIRECTORY + '/sha1_index'
        cache_file = generate_cache_file_name(url)
        with open(cache_file, 'xb') as fh:
            fh.write(raw_url_bytes)
        line_to_write = cache_file + ' ' + url + '\n'
        write_unique_line(line_to_write, cache_index_file)

    eprint("Returning %d bytes from %s", len(raw_url_bytes), url, level=LOG['DEBUG'])
    return raw_url_bytes

def extract_domain_set_from_hosts_format_bytes(hosts_format_bytes):
    assert isinstance(hosts_format_bytes, bytes)
    domains = set()
    hosts_format_bytes_lines = hosts_format_bytes.split(b'\n')
    for line in hosts_format_bytes_lines:
        line = line.replace(b'\t', b' ')         # expand tabs
        line = b' '.join(line.split())           # collapse whitespace
        line = line.strip()
        line = remove_comments_from_bytes(line)
        if b' ' in line:                         # hosts format
            # get DNS name (the url's are in hosts 0.0.0.0 dom.com format
            line = line.split(b' ')[1]
            # pylint: disable=bad-builtin
            # ignore leading/trailing .
            line = b'.'.join(list(filter(None, line.split(b'.'))))
            # pylint: enable=bad-builtin
            domains.add(line)
    return domains

def extract_domain_set_from_hosts_format_url(url, no_cache=False):
    url_bytes = read_url_bytes(url, no_cache)
    domains = extract_domain_set_from_hosts_format_bytes(url_bytes)
    eprint("Domains in %s:%s", url, len(domains), level=LOG['DEBUG'])
    return domains

def prune_redundant_rules(domains):
    domains_orig = copy.deepcopy(domains) # need to iterate through _orig later
    for domain in domains_orig:
        domain_parts_msb = list(reversed(domain.split(b'.'))) # start with the TLD
        for index in range(len(domain_parts_msb)):
            domain_to_check = b'.'.join(domain_parts_msb[0:index])
            if domain_to_check in domains_orig:
                eprint("removing: %s because it's parent domain: %s is already blocked",
                    domain, domain_to_check, level=LOG['DEBUG'])
                domains.remove(domain)

def is_broken_symlink(path):
    if os.path.islink(path):
        return not os.path.exists(path) # returns False for broken symlinks
    return False # path isnt a symlink

def is_unbroken_symlink(path):
    if os.path.islink(path): # path is a symlink
        return os.path.exists(path) # returns False for broken symlinks
    return False # path isnt a symlink

def get_symlink_abs_target(link): # assumes link is unbroken
    target = os.readlink(link)
    target_joined = os.path.join(os.path.dirname(link), target)
    target_file = os.path.realpath(target_joined)
    return target_file

def is_unbroken_symlink_to_target(target, link):    #bug, should not assume unicode paths
    if is_unbroken_symlink(link):
        link_target = get_symlink_abs_target(link)
        if link_target == target:
            return True
    return False

def path_exists(path):
    return os.path.lexists(path) #returns True for broken symlinks

def symlink_relative(target, link_name):
    target = os.path.abspath(target)
    link_name = os.path.abspath(link_name)
    if not path_exists(target):
        eprint('target: %s does not exist. Refusing to make broken symlink. Exiting.',
            target, level=LOG['ERROR'])
        quit(1)

    if is_broken_symlink(link_name):
        eprint('ERROR: %s exists as a broken symlink. ' +
            'Remove it before trying to make a new symlink. Exiting.',
            link_name, level=LOG['ERROR'])
        quit(1)

    link_name_folder = '/'.join(link_name.split('/')[:-1])
    if not os.path.isdir(link_name_folder):
        eprint('link_name_folder: %s does not exist. Exiting.',
            link_name_folder, level=LOG['ERROR'])
        quit(1)

    relative_target = os.path.relpath(target, link_name_folder)
    os.symlink(relative_target, link_name)

OUTPUT_FILE_HELP = '(for testing) output file (defaults to ' + DEFAULT_OUTPUT_FILE + ')'
DNSMASQ_CONFIG_HELP = 'dnsmasq config file (defaults to ' + DNSMASQ_CONFIG_FILE + ')'
BACKUP_HELP = 'backup output file before overwriting'
INSTALL_HELP_HELP = 'Help configure dnsmasq or /etc/hosts'
SOURCES_HELP = 'remote blacklist(s) to get rules from. Defaults to: ' + \
    ' '.join(DEFAULT_REMOTE_BLACKLISTS)
WHITELIST_HELP = '''\b
whitelists(s) defaults to:''' + CUSTOM_WHITELIST.replace(os.path.expanduser('~'), '~')
BLOCK_AT_PSL_HELP = 'strips subdomains, for example: analytics.google.com -> google.com' + \
    ' (must manually whitelist inadvertently blocked domains)'
VERBOSE_HELP = 'print debug information to stderr'
NO_CACHE_HELP = 'do not cache --source files as sha1(url) to ~/.dnsgate/cache/'
CACHE_EXPIRE_HELP = 'seconds until cached remote sources are re-downloaded ' + \
    '(defaults to ' + str(CACHE_EXPIRE / 3600) + ' hours)'
DEST_IP_HELP = 'IP to redirect blocked connections to (defaults to ' + \
    '127.0.0.1 in hosts mode, specifying this in dnsmasq mode causes ' + \
    'lookups to resolve rather than return NXDOMAIN)'
NO_RESTART_DNSMASQ_HELP = 'do not restart the dnsmasq service'
BLACKLIST_HELP = 'Add domain(s) to ' + CUSTOM_BLACKLIST
WHITELIST_HELP = 'Add domain(s) to ' + CUSTOM_WHITELIST
DISABLE_HELP = 'Disable ' + DEFAULT_OUTPUT_FILE
ENABLE_HELP = 'Enable ' + DEFAULT_OUTPUT_FILE
CONFIGURE_HELP = 'Write ' + CONFIG_FILE
GENERATE_HELP = 'Create ' + DEFAULT_OUTPUT_FILE

# https://github.com/mitsuhiko/click/issues/441
CONTEXT_SETTINGS = dict(help_option_names=['--help'],
    terminal_width=shutil.get_terminal_size((80, 20)).columns)

# pylint: disable=C0326
# http://pylint-messages.wikidot.com/messages:c0326
@click.group(context_settings=CONTEXT_SETTINGS)
@click.option('--no-restart-dnsmasq', is_flag=True,  help=NO_RESTART_DNSMASQ_HELP)
@click.option('--backup',          is_flag=True,  help=BACKUP_HELP)
@click.option('--verbose',         is_flag=True,  help=VERBOSE_HELP,
    callback=set_verbose, expose_value=False)
# pylint: enable=C0326
@click.pass_context
def dnsgate(ctx, no_restart_dnsmasq, backup):
    """
    dnsgate combines, deduplicates, and optionally modifies local and
    remote DNS blacklists. Use \"dnsgate (command) --help\"
    for more information.
    """
    config = configparser.ConfigParser()
    if 'dnsgate configure' not in ' '.join(sys.argv):
        if 'dnsgate.py configure' not in ' '.join(sys.argv):
            try:
                with open(CONFIG_FILE, 'r') as cf:
                    config.read_file(cf)
            except FileNotFoundError:
                eprint("No configuration file found, run " +
                    "\"dnsgate configure --help\". Exiting.", level=LOG['ERROR'])
                quit(1)

            mode = config['DEFAULT']['mode']
            block_at_psl = config['DEFAULT'].getboolean('block_at_psl')
            dest_ip = config['DEFAULT']['dest_ip'] # todo validate ip or False/None
            sources = ast.literal_eval(config['DEFAULT']['sources']) # because configparser has no .getlist()
            if mode == 'dnsmasq':
                try:
                    dnsmasq_config_file = \
                        click.open_file(config['DEFAULT']['dnsmasq_config_file'], 'w',
                            atomic=True, lazy=True)
                    dnsmasq_config_file.close()
                except KeyError:
                    eprint("ERROR: dnsgate is configured for 'mode = dnsmasq' in " +
                        CONFIG_FILE + " but dnsmasq_config_file is not set. " +
                        "run 'dnsmasq configure --help' to fix. Exiting.",
                        level=LOG['ERROR'])
                    quit(1)

                ctx.obj = Dnsgate_Config(mode=mode, block_at_psl=block_at_psl,
                    dest_ip=dest_ip, no_restart_dnsmasq=no_restart_dnsmasq,
                    dnsmasq_config_file=dnsmasq_config_file, backup=backup,
                    sources=sources)
            else:
                ctx.obj = Dnsgate_Config(mode=mode, block_at_psl=block_at_psl,
                    dest_ip=dest_ip, no_restart_dnsmasq=no_restart_dnsmasq,
                    backup=backup, sources=sources)

            if dest_ip == 'False':
                dest_ip = None

            os.makedirs(CACHE_DIRECTORY, exist_ok=True)

@dnsgate.command(help=WHITELIST_HELP)
@click.argument('domains', required=True, nargs=-1)
def whitelist(domains):
    for domain in domains:
        append_to_local_rule_file(CUSTOM_WHITELIST, domain)
    context = click.get_current_context()
    context.invoke(generate)

@dnsgate.command(help=BLACKLIST_HELP)
@click.argument('domains', required=True, nargs=-1)
def blacklist(domains):
    for domain in domains:
        append_to_local_rule_file(CUSTOM_BLACKLIST, domain)
    context = click.get_current_context()
    context.invoke(generate)

@dnsgate.command(help=INSTALL_HELP_HELP)
@click.pass_obj
def install_help(config):
    if config.mode == 'dnsmasq':
        dnsmasq_install_help(DNSMASQ_CONFIG_FILE)
    elif config.mode == 'hosts':
        hosts_install_help()
    quit(0)

@dnsgate.command(help=ENABLE_HELP)
@click.pass_obj
def enable(config):
    if config.mode == 'dnsmasq':
        # verify generate() was last run in dnsmasq mode so dnsmasq does not
        # fail when the service is restarted
        with open(DEFAULT_OUTPUT_FILE, 'r') as fh:
            file_content = fh.read(550) #just check the header
            if 'mode: dnsmasq' not in file_content:
                eprint('ERROR: %s was not generated in dnsmasq mode, ' +
                    'run "dnsgate generate --help" to fix. Exiting.',
                    DEFAULT_OUTPUT_FILE, level=LOG['ERROR'])
                quit(1)

        dnsmasq_config_line = generate_dnsmasq_config_file_line()
        if not uncomment_line_in_file(config.dnsmasq_config_file, dnsmasq_config_line):
            write_unique_line(dnsmasq_config_line, config.dnsmasq_config_file.name)

        config.dnsmasq_config_file.close()
        symlink = DNSMASQ_CONFIG_SYMLINK
        if not os.path.islink(symlink): # not a symlink
            if os.path.exists(symlink): # but exists
                eprint("ERROR: " + symlink + " exists and is not a symlink. " +
                    "You need to manually delete it. Exiting.", level=LOG['ERROR'])
                quit(1)
        if is_broken_symlink(symlink): #hm, a broken symlink, ok, remove it
            eprint("WARNING: removing broken symlink: %s", dnsmasq, level=LOG['WARNING'])
            os.remove(symlink)
        if not is_unbroken_symlink_to_target(DEFAULT_OUTPUT_FILE, symlink):
            try:
                os.remove(symlink) # maybe it was symlink to somewhere else
            except FileNotFoundError:
                pass    # that's ok
            symlink_relative(DEFAULT_OUTPUT_FILE, symlink)
        restart_dnsmasq_service()
    else:
        eprint("ERROR: enable is only available with --mode dnsmasq. Exiting.",
            level=LOG['ERROR'])
        quit(1)

@dnsgate.command(help=DISABLE_HELP)
@click.pass_obj
def disable(config):
    if config.mode == 'dnsmasq':
        comment_out_line_in_file(config.dnsmasq_config_file,
            generate_dnsmasq_config_file_line())
        config.dnsmasq_config_file.close()
        symlink = DNSMASQ_CONFIG_SYMLINK
        if os.path.islink(symlink):
            os.remove(symlink)
        if not os.path.islink(symlink): # not a symlink
            if os.path.exists(symlink): # but exists
                eprint("ERROR: " + symlink + " exists and is not a symlink. " +
                    "You need to manually delete it. Exiting.", level=LOG['ERROR'])
                quit(1)
        restart_dnsmasq_service()
    else:
        eprint("ERROR: disable is only available with --mode dnsmasq. Exiting.",
            level=LOG['ERROR'])
        quit(1)

@dnsgate.command(help=CONFIGURE_HELP)
@click.argument('sources',      nargs=-1)
@click.option('--mode',         is_flag=False,
    type=click.Choice(['dnsmasq', 'hosts']), required=True)
@click.option('--block-at-psl', is_flag=True,  help=BLOCK_AT_PSL_HELP)
@click.option('--dest-ip',      is_flag=False, help=DEST_IP_HELP, default=False)
@click.option('--dnsmasq-config-file', is_flag=False, help=DNSMASQ_CONFIG_HELP,
    type=click.File(mode='w', atomic=True, lazy=True), default=DNSMASQ_CONFIG_FILE)
def configure(sources, mode, block_at_psl, dest_ip, dnsmasq_config_file):
    if contains_whitespace(dnsmasq_config_file.name):
        eprint("ERROR: --dnsmasq-config-file can not contain whitespace. Exiting.",
            level=LOG['ERROR'])
        quit(1)

    if not sources:
        sources = DEFAULT_REMOTE_BLACKLISTS

    os.makedirs(CONFIG_DIRECTORY, exist_ok=True)
    config = configparser.ConfigParser()
    config['DEFAULT'] = \
        {
        'mode': mode,
        'block_at_psl': block_at_psl,
        'dest_ip': dest_ip,
        'sources': sources
        }

    if mode == 'dnsmasq':
        os.makedirs(DNSMASQ_CONFIG_INCLUDE_DIRECTORY, exist_ok=True)
        config['DEFAULT']['dnsmasq_config_file'] = dnsmasq_config_file.name

    with open(CONFIG_FILE, 'w') as cf:
        config.write(cf)

    if not os.path.exists(CUSTOM_BLACKLIST):
        with open(CUSTOM_BLACKLIST, 'w') as fh: # not 'wb', utf8 is ok
            fh.write(make_custom_blacklist_header(CUSTOM_BLACKLIST))

    if not os.path.exists(CUSTOM_WHITELIST):
        with open(CUSTOM_WHITELIST, 'w') as fh: # not 'wb', utf8 is ok
            fh.write(make_custom_whitelist_header(CUSTOM_WHITELIST))

@dnsgate.command(help=GENERATE_HELP)
@click.option('--no-cache',     is_flag=True,  help=NO_CACHE_HELP)
@click.option('--cache-expire', is_flag=False, help=CACHE_EXPIRE_HELP,
    type=int, default=CACHE_EXPIRE)
@click.option('--output',       is_flag=False, help=OUTPUT_FILE_HELP,
    type=click.File(mode='wb', atomic=True, lazy=True), default=DEFAULT_OUTPUT_FILE)
@click.pass_obj
def generate(config, no_cache, cache_expire, output):

    eprint('Using output file: %s', output.name, level=LOG['INFO'])
    config_dict = {
        'mode': config.mode,
        'sources': config.sources,
        'block_at_psl': config.block_at_psl,
        'no_cache': no_cache,
        'cache_expire': cache_expire,
        'dest_ip': config.dest_ip,
        'output': output.name
        }

    whitelist_file = os.path.abspath(CUSTOM_WHITELIST)
    try:
        domains_whitelist = extract_domain_set_from_dnsgate_format_file(whitelist_file)
    except FileNotFoundError:
        domains_whitelist = set()
        eprint('WARNING: %s is missing, only the default remote sources will be used.' +
            'Run "dnsgate configure --help" to fix.', CUSTOM_WHITELIST, level=LOG['WARNING'])
    else:
        if domains_whitelist:
            eprint("%d domains from %s", len(domains_whitelist),
                CUSTOM_WHITELIST, level=LOG['DEBUG'])
            domains_whitelist = validate_domain_list(domains_whitelist)
            eprint('%d validated whitelist domains.', len(domains_whitelist),
                level=LOG['INFO'])

    if not domains_whitelist:
        if config.block_at_psl:
            eprint('WARNING: block_at_psl is enabled in ' +
                CONFIG_FILE + ' and 0 domains were obtained from %s. ' +
                'If you get "Domain Not Found" errors, use "dnsgate whitelist --help"',
                CUSTOM_WHITELIST, level=LOG['WARNING'])

    domains_combined_orig = set()   # domains from all sources, combined
    eprint("Reading remote blacklist(s):\n%s", str(config.sources), level=LOG['INFO'])
    for item in config.sources:
        if item.startswith('http'):
            try:
                eprint("Trying http:// blacklist location: %s", item, level=LOG['DEBUG'])
                domains = extract_domain_set_from_hosts_format_url_or_cached_copy(item,
                    no_cache, cache_expire)
                if domains:
                    domains_combined_orig = domains_combined_orig | domains # union
                    eprint("len(domains_combined_orig): %s",
                        len(domains_combined_orig), level=LOG['DEBUG'])
                else:
                    print('ERROR: Failed to get ' + item + ', skipping.', level=LOG['ERROR'])
                    continue
            except Exception as e:
                eprint("Exception on blacklist url: %s", item, level=LOG['ERROR'])
                eprint(e, level=LOG['ERROR'])
        else:
            eprint('ERROR: ' + item +
                ' must start with http:// or https://, skipping.', level=LOG['ERROR'])

    eprint("%d domains from remote blacklist(s).",
        len(domains_combined_orig), level=LOG['INFO'])

    if len(domains_combined_orig) == 0:
        eprint("WARNING: 0 domains were retrieved from " +
            "remote sources, only the local " + CUSTOM_BLACKLIST +
            " will be used.", level=LOG['WARNING'])

    domains_combined_orig = validate_domain_list(domains_combined_orig)
    eprint('%d validated remote blacklisted domains.',
        len(domains_combined_orig), level=LOG['INFO'])

    domains_combined = copy.deepcopy(domains_combined_orig) # need to iterate through _orig later

    if config.block_at_psl and config.mode != 'hosts':
        domains_combined = strip_to_psl(domains_combined)
        eprint("%d blacklisted domains left after stripping to PSL domains.",
            len(domains_combined), level=LOG['INFO'])

        if domains_whitelist:
            eprint("Subtracting %d whitelisted domains.",
                len(domains_whitelist), level=LOG['INFO'])
            domains_combined = domains_combined - domains_whitelist
            eprint("%d blacklisted domains left after subtracting the whitelist.",
                len(domains_combined), level=LOG['INFO'])
            eprint('Iterating through the original %d whitelisted domains and ' +
                'making sure none are blocked by * rules.',
                len(domains_whitelist), level=LOG['INFO'])
            for domain in domains_whitelist:
                domain_psl = extract_psl_domain(domain)
                if domain_psl in domains_combined:
                    domains_combined.remove(domain_psl)

        # this needs to happen even if len(whitelisted_domains) == 0
        eprint('Iterating through original %d blacklisted domains to re-add subdomains' +
            ' that are not whitelisted', len(domains_combined_orig), level=LOG['INFO'])
        # re-add subdomains that are not explicitly whitelisted or already blocked
        for orig_domain in domains_combined_orig: # check every original full hostname
            if orig_domain not in domains_whitelist: # if it's not in the whitelist
                if orig_domain not in domains_combined: # and it's not in the current blacklist
                                                        # (almost none will be if --block-at-psl)
                    # get it's psl to see if it's already blocked
                    orig_domain_psl = extract_psl_domain(orig_domain)

                    if orig_domain_psl not in domains_combined: # if the psl is not already blocked
                        eprint("Re-adding: %s", orig_domain, level=LOG['DEBUG'])
                        domains_combined.add(orig_domain) # add the full hostname to the blacklist

        eprint('%d blacklisted domains after re-adding non-explicitly blacklisted subdomains',
            len(domains_combined), level=LOG['INFO'])

    elif config.block_at_psl and config.mode == 'hosts':
        eprint("ERROR: --block-at-psl is not possible in hosts mode. Exiting.",
            level=-LOG['ERROR'])
        quit(1)

    # apply whitelist before applying local blacklist
    domains_combined = domains_combined - domains_whitelist  # remove exact whitelist matches
    eprint("%d blacklisted domains after subtracting the %d whitelisted domains",
        len(domains_combined), len(domains_whitelist), level=LOG['INFO'])

    # must happen after subdomain stripping and after whitelist subtraction
    blacklist_file = os.path.abspath(CUSTOM_BLACKLIST)
    try:
        domains_blacklist = extract_domain_set_from_dnsgate_format_file(blacklist_file)
    except FileNotFoundError:
        domains_blacklist = set()
        eprint('WARNING: %s is missing, only the default remote sources ' +
            'will be used. Run "dnsgate configure --help" to fix.',
            CUSTOM_BLACKLIST, level=LOG['WARNING'])
    else:
        if domains_blacklist: # ignore empty blacklist
            eprint("Got %s domains from the CUSTOM_BLACKLIST: %s",
                len(domains_blacklist), blacklist_file, level=LOG['DEBUG'])
            eprint("Re-adding %d domains in the local blacklist %s to override the whitelist.",
                len(domains_blacklist), CUSTOM_BLACKLIST, level=LOG['INFO'])
            domains_combined = domains_combined | domains_blacklist # union
            eprint("%d blacklisted domains after re-adding the custom blacklist.",
                len(domains_combined), level=LOG['INFO'])

    eprint("Validating final domain blacklist.", level=LOG['DEBUG'])
    domains_combined = validate_domain_list(domains_combined)
    eprint('%d validated blacklisted domains.', len(domains_combined),
        level=LOG['DEBUG'])

    prune_redundant_rules(domains_combined)
    eprint('%d blacklisted domains after removing redundant rules.', len(domains_combined),
        level=LOG['INFO'])

    domains_combined = group_by_tld(domains_combined) # do last, returns sorted list
    eprint('Final blacklisted domain count: %d', len(domains_combined),
        level=LOG['INFO'])

    if config.backup: # todo: unit test
        backup_file_if_exists(output)

    if not domains_combined:
        eprint("The list of domains to block is empty, nothing to do, exiting.",
            level=LOG['INFO'])
        quit(1)

    for domain in domains_whitelist:
        domain_tld = extract_psl_domain(domain)
        if domain_tld in domains_combined:
            eprint('WARNING: %s is listed in both %s and %s, '
                'the local blacklist always takes precedence.', domain.decode('UTF8'),
                CUSTOM_BLACKLIST, CUSTOM_WHITELIST, level=LOG['WARNING'])

    eprint("Writing output file: %s in %s format", output.name, config.mode, level=LOG['INFO'])

    output.write(make_output_file_header(config_dict))

    for domain in domains_combined:
        if config.mode == 'dnsmasq':
            if config.dest_ip:
                dnsmasq_line = 'address=/.' + domain.decode('utf8') + '/' + config.dest_ip + '\n'
            else:
                dnsmasq_line = 'server=/.' + domain.decode('utf8') + '/' '\n'  # return NXDOMAIN
            output.write(dnsmasq_line.encode('utf8'))
        elif config.mode == 'hosts':
            if config.dest_ip:
                hosts_line = config.dest_ip + ' ' + domain.decode('utf8') + '\n'
            else:
                hosts_line = '127.0.0.1' + ' ' + domain.decode('utf8') + '\n'
            output.write(hosts_line.encode('utf8'))

    output.close() # make sure file is written before restarting dnsmasq

    if not config.no_restart_dnsmasq:
        if config.mode != 'hosts':
            restart_dnsmasq_service()

if __name__ == '__main__':
    # pylint: disable=no-value-for-parameter
    dnsgate()
    # pylint: enable=no-value-for-parameter
    eprint("Exiting without error.", level=LOG['DEBUG'])
    quit(0)
