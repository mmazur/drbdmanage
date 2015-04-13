#!/usr/bin/python
"""
    drbdmanage - management of distributed DRBD9 resources
    Copyright (C) 2013, 2014   LINBIT HA-Solutions GmbH
                               Author: R. Altnoeder

    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""

import os
import sys
import time
import json
import errno
import logging
import subprocess
import drbdmanage.storage.storagecore
import drbdmanage.storage.persistence as storpers

from drbdmanage.consts import DEFAULT_VG
from drbdmanage.exceptions import PersistenceException
from drbdmanage.exceptions import DM_ENOENT, DM_ESTORAGE, DM_SUCCESS, DM_DEBUG
from drbdmanage.utils import DataHash
from drbdmanage.utils import (build_path, read_lines)
from drbdmanage.conf.conffile import ConfFile

# from drbdmanage.storage.storagecore import StoragePlugin


#class LVM(drbdmanage.storage.storagecore.StoragePlugin):
class LVM(object):

    """
    LVM logical volume backing store plugin for the drbdmanage server

    Provides backing store block devices for DRBD volumes by managing the
    allocation of logical volumes inside a volume group of the
    logical volume manager (LVM).
    """

    KEY_DEV_PATH  = "dev-path"
    KEY_VG_NAME   = "volume-group"
    KEY_LVM_PATH  = "lvm-path"

    LVM_CONFFILE = "/etc/drbdmanaged-lvm.conf"
    LVM_SAVEFILE = "/var/lib/drbdmanage/drbdmanaged-lvm.local.json"

    LVM_CREATE   = "lvcreate"
    LVM_REMOVE   = "lvremove"
    LVM_LVS      = "lvs"
    LVM_VGS      = "vgs"

    # LV exists error code
    LVM_EEXIST   = 5

    # lvs exit code if the LV was not found
    LVM_LVS_ENOENT = 5

    # Delay (in seconds) for lvcreate/lvremove retries
    RETRY_DELAY = 1

    # Maximum number of retries
    MAX_RETRIES = 2

    CONF_DEFAULTS = {
      KEY_DEV_PATH : "/dev",
      KEY_VG_NAME  : DEFAULT_VG,
      KEY_LVM_PATH : "/sbin"
    }

    _lvs  = None
    _conf = None


    def __init__(self):
        super(LVM, self).__init__()
        try:
            self._lvs   = {}
            conf_loaded = None
            try:
                self.load_state()
            except PersistenceException:
                logging.warning(
                    "LVM plugin: Cannot load state file '%s'"
                    % (self.LVM_SAVEFILE)
                )
            try:
                conf_loaded = self.load_conf()
            except IOError:
                logging.warning(
                    "LVM plugin: Cannot load configuration file '%s'"
                    % (self.LVM_CONFFILE)
                )
            if conf_loaded is None:
                self._conf = self.CONF_DEFAULTS
            else:
                self._conf = ConfFile.conf_defaults_merge(
                    self.CONF_DEFAULTS, conf_loaded
                )
        except Exception as exc:
            logging.error(
                "LVM plugin: initialization failed, unhandled exception: %s"
                % (str(exc))
            )


    def _subproc_env(self):
        env = dict(os.environ.items())
        env['LC_ALL'] = 'C'
        env['LANG']   = 'C'
        return env


    def create_blockdevice(self, name, vol_id, size):
        """
        Allocates a block device as backing storage for a DRBD volume

        @param   name: resource name; subject to name constraints
        @type    name: str
        @param   id: volume id
        @type    id: int
        @param   size: size of the block device in kiB (binary kilobytes)
        @type    size: long
        @return: block device of the specified size
        @rtype:  BlockDevice object; None if the allocation fails
        """
        blockdev = None
        storcore = drbdmanage.storage.storagecore
        lv_name = self._lv_name(name, vol_id)
        try:
            tries = 0
            while tries < LVM.MAX_RETRIES:
                try:
                    # Check whether an LV with that name exists already
                    lv_exists = self._check_lv_exists(lv_name)
                    if lv_exists:
                        # LV exists, maybe from an earlier attempt at creating
                        # the volume. Remove existing LV and recreate it.
                        logging.warning(
                            "LVM plugin: Attempt %d of %d: "
                            "LV '%s' exists already, attempting to "
                            "remove and recreate the LV"
                            % (tries + 1, LVM.MAX_RETRIES, lv_name)
                        )
                        self._remove_lv(lv_name)

                    # Proceed with creating the LV
                    self._create_lv(lv_name, size)
                    # Check whether the LV was actually created
                    lv_exists = self._check_lv_exists(lv_name)
                    if lv_exists:
                        # LVM reports that the LV exists, create the
                        # blockdevice object representing the LV in
                        # drbdmanage and register it in the LVM module's
                        # persistent data structures
                        blockdev = storcore.BlockDevice(
                            lv_name, size,
                            self._lv_path_prefix() + lv_name
                        )
                        self._lvs[lv_name] = blockdev
                        self.save_state()
                        break
                    else:
                        logging.error(
                            "LVM plugin: Attempt %d of %d: "
                            "Creation of LV '%s' failed."
                            % (tries + 1, LVM.MAX_RETRIES, lv_name)
                        )
                except LVMException:
                    # Check for existing LVs failed, retry
                    logging.error(
                        "LVM plugin: Unable to retrieve the list "
                        "of existing LVs"
                    )
                    pass
                tries += 1
        except Exception as exc:
            logging.error(
                "LVM plugin: Block device creation failed, "
                "unhandled exception: %s"
                % (str(exc))
            )
        return blockdev


    def remove_blockdevice(self, blockdevice):
        """
        Deallocates a block device

        @param   blockdevice: the block device to deallocate
        @type    blockdevice: BlockDevice object
        @return: standard return code (see drbdmanage.exceptions)
        """
        fn_rc = DM_ESTORAGE
        tries = 0
        lv_name = blockdevice.get_name()
        while tries < LVM.MAX_RETRIES:
            # Attempt to remove the LV
            self._remove_lv(lv_name)
            # Check whether the LV is still there
            try:
                lv_exists = self._check_lv_exists(lv_name)
                if lv_exists:
                    # LV still exists, removal failed
                    logging.warning(
                        "LVM plugin: Attempt %d of %d: "
                        "Unable to remove LV '%s'"
                        % (tries + 1, LVM.MAX_RETRIES, lv_name)
                    )
                else:
                    # LV removal successful
                    try:
                        del self._lvs[blockdevice.get_name()]
                    except KeyError:
                        # Being unable to remove an element that is not there
                        # in the first place is irrelevant, ignore error
                        pass
                    self.save_state()
                    fn_rc = DM_SUCCESS
                    break
            except LVMException:
                # Check for existing LVs failed, retry
                logging.error(
                    "LVM plugin: Unable to retrieve the list "
                    "of existing LVs"
                )
                pass
            tries += 1
        return fn_rc


    def create_snapshot(self, name, vol_id, src_bd_name):
        raise NotImplementedError


    def remove_snapshot(self, blockdev):
        raise NotImplementedError


    def get_blockdevice(self, bd_name):
        """
        Retrieves a registered BlockDevice object

        The BlockDevice object allocated and registered under the supplied
        resource name and volume id is returned.

        @return: the specified block device; None on error
        @rtype:  BlockDevice object
        """
        blockdev = None
        try:
            blockdev = self._lvs[bd_name]
        except KeyError:
            pass
        return blockdev


    def up_blockdevice(self, blockdev):
        """
        Activates a block device (e.g., connects an iSCSI resource)

        @param blockdevice: the block device to deactivate
        @type  blockdevice: BlockDevice object
        """
        return DM_SUCCESS


    def down_blockdevice(self, blockdev):
        """
        Deactivates a block device (e.g., disconnects an iSCSI resource)

        @param blockdevice: the block device to deactivate
        @type  blockdevice: BlockDevice object
        """
        return DM_SUCCESS


    def update_pool(self, node):
        """
        Updates the DrbdNode object with the current storage status

        Determines the current total and free space that is available for
        allocation on the host this instance of the drbdmanage server is
        running on and updates the DrbdNode object with that information.

        @param   node: The node to update
        @type    node: DrbdNode object
        @return: standard return code (see drbdmanage.exceptions)
        """
        fn_rc    = DM_ESTORAGE
        poolsize = -1
        poolfree = -1

        vgs = self._lv_command_path(self.LVM_VGS)
        lvm_proc = None

        try:
            lvm_proc = subprocess.Popen(
                [vgs, "--noheadings", "--nosuffix", "--units", "k",
                 "--separator", ",", "--options", "vg_size,vg_free",
                 self._conf[self.KEY_VG_NAME]],
                env=self._subproc_env(),
                stdout=subprocess.PIPE, close_fds=True
            )
            pool_str = lvm_proc.stdout.readline()
            if pool_str is not None:
                pool_str = pool_str.strip()
                idx = pool_str.find(",")
                if idx != -1:
                    size_str = pool_str[:idx]
                    free_str = pool_str[idx + 1:]
                    idx = size_str.find(".")
                    if idx != -1:
                        size_str = size_str[:idx]
                    idx = free_str.find(".")
                    if idx != -1:
                        free_str = free_str[:idx]
                    try:
                        poolsize = long(size_str)
                        poolfree = long(free_str)
                    except ValueError:
                        poolsize = -1
                        poolfree = -1
                    fn_rc = DM_SUCCESS
        finally:
            if lvm_proc is not None:
                try:
                    lvm_proc.stdout.close()
                except Exception:
                    pass
                lvm_proc.wait()
        return (fn_rc, poolsize, poolfree)


    def _create_lv(self, name, size):
        lvcreate = self._lv_command_path(self.LVM_CREATE)

        lvm_proc = subprocess.Popen(
            [lvcreate, "-n", name, "-L", str(size) + "k",
             self._conf[self.KEY_VG_NAME]],
            0, lvcreate,
            env=self._subproc_env(), close_fds=True
          )
        fn_rc = lvm_proc.wait()
        return fn_rc


    def _remove_lv(self, name):
        lvremove = self._lv_command_path(self.LVM_REMOVE)

        lvm_proc = subprocess.Popen(
            [lvremove, "--force", self._conf[self.KEY_VG_NAME] + "/" + name],
            0, lvremove,
            env=self._subproc_env(), close_fds=True
        )
        fn_rc = lvm_proc.wait()
        if fn_rc != 0:
            time.sleep(LVM.RETRY_DELAY)
        return fn_rc


    def _lv_command_path(self, cmd):
        return build_path(self._conf[self.KEY_LVM_PATH], cmd)


    def _lv_name(self, name, vol_id):
        return ("%s_%.2d" % (name, vol_id))


    def _lv_path_prefix(self):
        vg_name  = self._conf[self.KEY_VG_NAME]
        dev_path = self._conf[self.KEY_DEV_PATH]
        return build_path(dev_path, vg_name) + "/"


    def _check_lv_exists(self, name):
        # Check whether an LVM logical volume exists
        #
        # @returns: True if the LV exists, False if the LV does not exist
        # Throws an LVMException if the check itself fails
        lvcheck = self._lv_command_path(self.LVM_LVS)

        exists = False

        try:
            lvm_proc = subprocess.Popen(
                [
                    lvcheck, "--noheadings", "--options", "lv_name",
                    self._conf[self.KEY_VG_NAME] + "/" + name
                ],
                0, lvcheck,
                env=self._subproc_env(), stdout=subprocess.PIPE,
                close_fds=True
            )
            lventry = lvm_proc.stdout.readline()
            if len(lventry) > 0:
                lventry = lventry[:-1].strip()
                if lventry == name:
                    exists = True
            lvm_rc = lvm_proc.wait()
            # lvs exits with exit code 5 if the LV was not found
            if lvm_rc != 0 and lvm_rc != self.LVM_LVS_ENOENT:
                raise LVMException
        except OSError:
            raise LVMException

        return exists


    def load_conf(self):
        in_file = None
        conf = None
        try:
            in_file = open(self.LVM_CONFFILE, "r")
            conffile = ConfFile(in_file)
            conf = conffile.get_conf()
        except IOError as ioerr:
            if ioerr.errno == errno.EACCES:
                logging.error(
                    "LVM plugin: cannot open configuration file "
                    "'%s': Permission denied"
                    % (self.LVM_CONFFILE)
                )
            elif ioerr.errno != errno.ENOENT:
                logging.error(
                    "LVM plugin: cannot open configuration file "
                    "'%s', error returned by the OS is: %s"
                    % (self.LVM_CONFFILE, ioerr.strerror)
                )
        finally:
            if in_file is not None:
                in_file.close()
        return conf


    def load_state(self):
        in_file = None
        try:
            stored_hash = None
            in_file = open(self.LVM_SAVEFILE, "r")
            offset = 0
            for line in read_lines(in_file):
                if line.startswith("sig:"):
                    stored_hash = line[4:]
                    if stored_hash.endswith("\n"):
                        stored_hash = stored_hash[:len(stored_hash) - 1]
                    break
                else:
                    offset = in_file.tell()
            in_file.seek(0)
            if offset != 0:
                load_data = in_file.read(offset)
            else:
                load_data = in_file.read()
            if stored_hash is not None:
                data_hash = DataHash()
                data_hash.update(load_data)
                computed_hash = data_hash.get_hex_hash()
                if computed_hash != stored_hash:
                    logging.warning(
                        "LVM plugin: state data does not match its signature"
                    )
            lvm_con = json.loads(load_data)
            for properties in lvm_con.itervalues():
                blockdev = storpers.BlockDevicePersistence.load(properties)
                if blockdev is not None:
                    self._lvs[blockdev.get_name()] = blockdev
        except Exception:
            raise PersistenceException
        finally:
            if in_file is not None:
                in_file.close()


    def save_state(self):
        lvm_con = {}
        for blockdev in self._lvs.itervalues():
            bd_persist = storpers.BlockDevicePersistence(blockdev)
            bd_persist.save(lvm_con)
        out_file = None
        try:
            out_file = open(self.LVM_SAVEFILE, "w")
            data_hash = DataHash()
            save_data = json.dumps(lvm_con, indent=4, sort_keys=True) + "\n"
            data_hash.update(save_data)
            out_file.write(save_data)
            out_file.write("sig:" + data_hash.get_hex_hash() + "\n")
        except Exception as exc:
            logging.error(
                "LVM plugin: saving state data failed, unhandled exception: %s"
                % str(exc)
            )
            raise PersistenceException
        finally:
            if out_file is not None:
                out_file.close()


    def reconfigure(self):
        """
        Reconfigures the storage plugin
        """
        pass


class LVMException(Exception):

    def __init__(self):
        super(LVMException, self).__init__()
