#! /usr/bin/python -u

from argparse import ArgumentParser
from datetime import datetime
from enum import IntEnum
import os
import re
import sys
import time
import threading
import traceback
import serial
import gobject
import dbus
import dbus.mainloop.glib
from vedbus import VeDbusService
from settingsdevice import SettingsDevice

import logging
log = logging.getLogger()

NAME = 'dbus-hiber'
VERSION = '0.1'

hiber_settings = {
    'faker': ['/Settings/Hiber/Faker', 0, 0, 1],
}

GPIO_RESET = 4
GPIO_WAKEUP = 5
GPIO_WDT = 6

def sysfs_read(file):
    fd = os.open(file, os.O_RDONLY)
    line = os.read(fd, 1024)
    os.close(fd)
    return line.strip()

def sysfs_write(file, val):
    fd = os.open(file, os.O_WRONLY)
    os.write(fd, str(val))
    os.close(fd)

class Gpio(object):
    def __init__(self, nr, direction):
        sysfs = '/sys/class/gpio/gpio%d' % nr;
        try:
            os.stat(sysfs)
        except OSError:
            sysfs_write('/sys/class/gpio/export', nr)

        sysfs_write(os.path.join(sysfs, 'direction'), direction)
        self.sysfs_value = os.path.join(sysfs, 'value')
        self.get()

    def get(self):
        val = sysfs_read(self.sysfs_value)
        self.value = int(val)
        return self.value

    def set(self, val):
        sysfs_write(self.sysfs_value, val)
        self.value = val

def find_gpio_base(tty):
    try:
        dir = os.listdir(os.path.join('/sys/class/tty', tty, 'device/../gpio'))
        base = sysfs_read(os.path.join('/sys/class/gpio', dir[0], 'base'));
        return int(base)
    except:
        return None

def abstime(offset):
    atime = None

    try:
        offset = int(offset)
        if offset >= 0:
            atime = int(time.time()) + offset
    except ValueError:
        pass

    return atime

class Hiber(object):
    def __init__(self, dbus, dev, rate, gpio_base):
        self.dbus = dbus
        self.lock = threading.Lock()
        self.thread = None
        self.ser = None
        self.dev = dev
        self.rate = rate
        self.cmds = []
        self.lastcmd = None
        self.lastwake = 0
        self.nextpass = None
        self.ready = False
        self.reset = Gpio(gpio_base + GPIO_RESET, 'out')
        self.wakeup = Gpio(gpio_base + GPIO_WAKEUP, 'out')
        self.wdt = Gpio(gpio_base + GPIO_WDT, 'out')

    def error(self, msg):
        global mainloop

        log.error('%s, quitting' % msg)
        mainloop.quit()

    def send(self, cmd):
        self.lastcmd = cmd
        self.ready = False

        log.debug('> %s' % cmd)

        try:
            self.ser.write(cmd + '\r\n')
        except serial.SerialException:
            self.error('Write error')

    def cmd(self, cmds):
        with self.lock:
            if self.ready and not self.cmds:
                self.send(cmds.pop(0))

            self.cmds += cmds

            if not self.wakeup.value:
                self.wakeup.set(1)

    def handle_resp(self, cmd, code, vals):
        if code == 602:
            log.debug('Modem sleeping')
            return False

        if not cmd:
            log.warn('Unexpected response: ')
            return True

        cmd = cmd.split('(', 1)[0]

        if cmd == 'get_firmware_version':
            self.dbus['/Firmware'] = vals[0]
            return True

        if cmd == 'get_modem_info':
            self.dbus['/Model'] = vals[0]
            self.dbus['/ModemNumber'] = vals[3]
            return True

        if cmd == 'get_datetime':
            log.info('Modem time: %s' % vals[0])
            return True

        if cmd == 'get_location':
            log.info('Modem location: %s %s' % (vals[0], vals[1]))
            return True

        if cmd == 'get_next_alarm':
            self.dbus['/NextAlarm'] = abstime(vals[1])
            return True

        if cmd == 'get_next_pass':
            self.nextpass = abstime(vals[0])
            self.dbus['/NextPass'] = self.nextpass
            return True

        return True

    def run(self):
        re_ready = re.compile(r'^Hiber API .* - Ready$')
        re_api = re.compile(r'^API\(([0-9]+)(?:: *(.*))?\)$')

        self.wakeup.set(0)
        self.reset.set(0)
        time.sleep(0.1)
        self.wakeup.set(1)

        self.ser.timeout = None

        self.cmd([
            'get_firmware_version()',
            'get_modem_info()',
            'get_datetime()',
            'get_location()',
            'set_gps_mode(0)',
            'get_next_alarm()',
            'get_next_pass()',
        ])

        self.set_faker()

        while True:
            with self.lock:
                if self.ready:
                    if self.cmds:
                        self.send(self.cmds.pop(0))
                    else:
                        self.wakeup.set(0)
                        self.send('go_to_sleep()')

            try:
                line = self.ser.readline().strip()
            except serial.SerialException:
                self.error('Read error')
                break

            if not line:
                continue

            log.debug('< %s' % line)

            cmd = self.lastcmd
            self.lastcmd = None

            m = re_ready.match(line)
            if m:
                log.debug('Modem awake')
                self.lastwake = time.time()
                self.ready = True
                continue

            m = re_api.match(line)
            if not m:
                log.warn('Unknown response: %s' % line)
                self.ready = True
                continue

            (code, vals) = m.groups()
            if vals:
                vals = re.split(r'; *', vals)

            try:
                code = int(code)
            except ValueError:
                log.error('Expected numeric result code: %s' % line)
                self.ready = True
                continue

            if code < 600:
                log.error('%s: error %d' % (cmd, code))
                self.ready = True
                continue

            self.ready = self.handle_resp(cmd, code, vals)

        quit(1)

    def set_faker(self):
        faker = self.settings['faker']
        self.cmd(['toggle_payload_over_debug(%d)' % faker])

    def setting_changed(self, setting, old, new):
        if setting == 'faker':
            self.set_faker()
            return

    def start(self):
        log.info('Waiting for localsettings')
        self.settings = SettingsDevice(self.dbus.dbusconn, hiber_settings,
                                       self.setting_changed, timeout=10)

        self.ser = serial.Serial(self.dev, self.rate)

        self.thread = threading.Thread(target=self.run)
        self.thread.start()

    def update_modem(self):
        self.cmd([
            'get_next_alarm()',
            'get_next_pass()',
        ])
        return True

    def update_watchdog(self):
        if time.time() - self.lastwake < 120:
            self.wdt.set(self.wdt.value ^ 1)
        else:
            self.reset.set(1)

        return True

def quit(n):
    global start
    log.info('End. Run time %s' % str(datetime.now() - start))
    os._exit(n)

def main():
    global mainloop
    global start

    start = datetime.now()

    parser = ArgumentParser(description=NAME, add_help=True)
    parser.add_argument('-d', '--debug', help='enable debug logging',
                        action='store_true')
    parser.add_argument('-s', '--serial', help='tty')

    args = parser.parse_args()

    logging.basicConfig(format='%(levelname)-8s %(message)s',
                        level=(logging.DEBUG if args.debug else logging.INFO))

    logLevel = {
        0:  'NOTSET',
        10: 'DEBUG',
        20: 'INFO',
        30: 'WARNING',
        40: 'ERROR',
    }
    log.info('Loglevel set to ' + logLevel[log.getEffectiveLevel()])

    if not args.serial:
        log.error('No serial port specified, see -h')
        exit(1)

    rate = 19200

    gpio_base = find_gpio_base(os.path.basename(args.serial))
    if gpio_base == None:
        log.error('GPIO not found')
        exit(1)

    log.info('Starting %s %s on %s at %d bps, GPIO base %d' %
             (NAME, VERSION, args.serial, rate, gpio_base))

    gobject.threads_init()
    dbus.mainloop.glib.threads_init()
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

    mainloop = gobject.MainLoop()

    svc = VeDbusService('com.victronenergy.hiber')

    svc.add_path('/Model', None)
    svc.add_path('/ModemNumber', None)
    svc.add_path('/Firmware', None)
    svc.add_path('/NextAlarm', None)
    svc.add_path('/NextPass', None)

    hiber = Hiber(svc, args.serial, rate, gpio_base)
    hiber.start()

    gobject.timeout_add(60000, hiber.update_modem)
    gobject.timeout_add(5000, hiber.update_watchdog)
    mainloop.run()

    quit(1)

try:
    main()
except KeyboardInterrupt:
    os._exit(1)
