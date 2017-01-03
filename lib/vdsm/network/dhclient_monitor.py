# Copyright 2013-2014 Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA
#
# Refer to the README and COPYING files for full details of the license
#
from __future__ import absolute_import

from contextlib import contextmanager
import logging
import os
import shlex
import threading

import pyinotify

from vdsm.constants import P_VDSM_RUN
from vdsm import utils

from .configurators.iproute2 import Iproute2
from .sourceroute import DynamicSourceRoute


ACTION_KEY = 'action'
IPADDR_KEY = 'ip'
IPMASK_KEY = 'mask'
IPROUTE_KEY = 'route'
IFACE_KEY = 'iface'


MONITOR_FOLDER = P_VDSM_RUN + 'sourceRoutes'
configurator = Iproute2()


def start():
    thread = threading.Thread(target=_monitor_dhcp_responses,
                              name='dhclient-monitor')
    thread.daemon = True
    thread.start()


class DHClientEventHandler(pyinotify.ProcessEvent):
    def process_IN_CLOSE_WRITE(self, event):
        _dhcp_response_handler(event.pathname)


def _dhcp_response_handler(data_filepath):
    with _cleaning_file(data_filepath):
        dhcp_response = _dhcp_response_data(data_filepath)

        action = dhcp_response.get(ACTION_KEY)
        device = dhcp_response.get(IFACE_KEY)

        if device is None:
            logging.warning('DHCP response with no device')
            return

        logging.debug('Received DHCP response for %s/%s', action, device)

        if DynamicSourceRoute.isVDSMInterface(device):
            _process_dhcp_response_actions(action, device, dhcp_response)
        else:
            logging.info('Interface %s is not a libvirt interface', device)

        DynamicSourceRoute.removeInterfaceTracking(device)


@contextmanager
def _cleaning_file(filepath):
    try:
        yield
    finally:
        os.remove(filepath)


def _process_dhcp_response_actions(action, device, dhcp_response):
    if action == 'configure':
        _dhcp_configure_action(dhcp_response, device)
    else:
        _dhcp_remove_action(device)


def _dhcp_configure_action(dhcp_response, device):
    ip = dhcp_response.get(IPADDR_KEY)
    mask = dhcp_response.get(IPMASK_KEY)
    gateway = dhcp_response.get(IPROUTE_KEY)

    if ip and mask and gateway not in (None, '0.0.0.0'):
        DynamicSourceRoute(
            device, configurator, ip, mask, gateway).configure()
    else:
        logging.warning('Partial DHCP response %s', dhcp_response)


def _dhcp_remove_action(device):
    DynamicSourceRoute(device, configurator, None, None, None).remove()


@utils.traceback()
def _monitor_dhcp_responses():
    logging.debug('Starting to monitor dhcp responses')

    # Subscribe to pyinotify event
    watchManager = pyinotify.WatchManager()
    handler = DHClientEventHandler()
    notifier = pyinotify.Notifier(watchManager, handler)
    watchManager.add_watch(MONITOR_FOLDER, pyinotify.IN_CLOSE_WRITE)

    # Run once manually in case dhclient operated while supervdsm was down
    # Run sorted so that if multiple files exist for an interface, we'll
    # execute them alphabetically and thus according to their time stamp
    for filePath in sorted(os.listdir(MONITOR_FOLDER)):
        _dhcp_response_handler(MONITOR_FOLDER + '/' + filePath)

    notifier.loop()


def _dhcp_response_data(fpath):
    data = {}
    try:
        with open(fpath) as f:
            for line in shlex.split(f):
                k, v = line.split('=', 1)
                data[k] = v
    except:
        logging.exception('Error reading dhcp response file {}'.format(fpath))
    return data