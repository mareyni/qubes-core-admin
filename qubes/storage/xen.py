#!/usr/bin/python2 -O
# vim: fileencoding=utf-8

#
# The Qubes OS Project, https://www.qubes-os.org/
#
# Copyright (C) 2015  Joanna Rutkowska <joanna@invisiblethingslab.com>
# Copyright (C) 2013-2015  Marek Marczykowski-Górecki
#                              <marmarek@invisiblethingslab.com>
# Copyright (C) 2015  Wojtek Porczyk <woju@invisiblethingslab.com>
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
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#

from __future__ import absolute_import

import os
import os.path
import re
import subprocess

from qubes.storage import Pool, StoragePoolException, Volume

BLKSIZE = 512


class XenPool(Pool):
    ''' File based 'original' disk implementation '''

    def __init__(self, name=None, dir_path=None):
        super(XenPool, self).__init__(name=name)
        assert dir_path, "No pool dir_path specified"
        self.dir_path = os.path.normpath(dir_path)

        create_dir_if_not_exists(self.dir_path)
        appvms_path = os.path.join(self.dir_path, 'appvms')
        create_dir_if_not_exists(appvms_path)
        vm_templates_path = os.path.join(self.dir_path, 'vm-templates')
        create_dir_if_not_exists(vm_templates_path)

    def create(self, volume, source_volume=None):
        _type = volume.volume_type
        size = volume.size
        if _type == 'origin':
            create_sparse_file(volume.path_origin, size)
            create_sparse_file(volume.path_cow, size)
        elif _type in ['read-write'] and source_volume:
            copy_file(source_volume.path, volume.path)
        elif _type in ['read-write', 'volatile']:
            create_sparse_file(volume.path, size)

        return volume

    def resize(self, volume, size):
        ''' Expands volume, throws
            :py:class:`qubst.storage.StoragePoolException` if given size is
            less than current_size
        '''
        _type = volume.volume_type
        if _type not in ['origin', 'read-write', 'volatile']:
            raise StoragePoolException('Can not resize a %s volume %s' %
                                       (_type, volume.vid))

        if size <= volume.size:
            raise StoragePoolException(
                'For your own safety, shrinking of %s is'
                ' disabled. If you really know what you'
                ' are doing, use `truncate` on %s manually.' %
                (volume.name, volume.vid))

        if _type == 'origin':
            path = volume.path_origin
        elif _type in ['read-write', 'volatile']:
            path = volume.path

        if size <= volume.size:
            raise StoragePoolException('Can not shring volume %s' %
                                       volume.name)

        with open(path, 'a+b') as fd:
            fd.truncate(size)

        # find loop device if any
        p = subprocess.Popen(['sudo', 'losetup', '--associated', path],
                             stdout=subprocess.PIPE)
        result = p.communicate()

        m = re.match(r'^(/dev/loop\d+):\s', result[0])
        if m is not None:
            loop_dev = m.group(1)

            # resize loop device
            subprocess.check_call(['sudo', 'losetup', '--set-capacity',
                                   loop_dev])

    def commit_template_changes(self, volume):
        if volume.volume_type != 'origin':
            return volume

        if os.path.exists(volume.path_cow):
            os.rename(volume.path_cow, volume.path_cow + '.old')

        old_umask = os.umask(002)
        with open(volume.path_cow, 'w') as f_cow:
            f_cow.truncate(volume.size)
        os.umask(old_umask)
        return volume

    def start(self, volume):
        if volume.volume_type == 'volatile':
            self._reset_volume(volume)
        if volume.volume_type in ['origin', 'snapshot']:
            _check_path(volume.path_origin)
            _check_path(volume.path_cow)
        else:
            _check_path(volume.path)

        return volume

    def stop(self, volume):
        pass

    def _reset_volume(self, volume):
        ''' Remove and recreate a volatile volume '''
        assert volume.volume_type == 'volatile', "Not a volatile volume"

        assert volume.size

        _remove_if_exists(volume)

        with open(volume.path, "w") as f_volatile:
            f_volatile.truncate(volume.size)
        return volume

    def target_dir(self, vm):
        """ Returns the path to vmdir depending on the type of the VM.

            The default QubesOS file storage saves the vm images in three
            different directories depending on the ``QubesVM`` type:

            * ``appvms`` for ``QubesAppVm`` or ``QubesHvm``
            * ``vm-templates`` for ``QubesTemplateVm`` or ``QubesTemplateHvm``

            Args:
                vm: a QubesVM
                pool_dir: the root directory of the pool

            Returns:
                string (str) absolute path to the directory where the vm files
                             are stored
        """
        if vm.is_template():
            subdir = 'vm-templates'
        elif vm.is_disposablevm():
            subdir = 'appvms'
            return os.path.join(self.dir_path, subdir,
                                vm.template.name + '-dvm')
        else:
            subdir = 'appvms'

        return os.path.join(self.dir_path, subdir, vm.name)

    def init_volume(self, vm, volume_config):
        assert 'volume_type' in volume_config, "Volume type missing " \
            + str(volume_config)
        volume_type = volume_config['volume_type']
        known_types = {
            'read-write': ReadWriteFile,
            'read-only': ReadOnlyFile,
            'origin': OriginFile,
            'snapshot': SnapshotFile,
            'volatile': VolatileFile,
        }
        if volume_type not in known_types:
            raise StoragePoolException("Unknown volume type " + volume_type)

        if volume_type in ['snapshot', 'read-only']:
            origin_pool = vm.app.get_pool(volume_config['pool'])
            assert isinstance(origin_pool,
                              XenPool), 'Origin volume not a xen volume'
            volume_config['target_dir'] = origin_pool.target_dir(vm.template)
            name = volume_config['name']
            volume_config['size'] = vm.template.volume_config[name]['size']
        else:
            volume_config['target_dir'] = self.target_dir(vm)

        return known_types[volume_type](**volume_config)


class XenVolume(Volume):
    ''' Parent class for the xen volumes implementation '''

    def __init__(self, target_dir, **kwargs):
        self.target_dir = target_dir
        assert self.target_dir, "target_dir not specified"
        super(XenVolume, self).__init__(**kwargs)


class SizeMixIn(XenVolume):
    ''' A mix in which expects a `size` param to be > 0 on initialization and
        provides a usage property wrapper.
    '''
    def __init__(self, name=None, pool=None, vid=None, target_dir=None, size=0,
                 **kwargs):
        assert size > 0, 'Size for volume ' + name + ' is <=0'
        super(SizeMixIn, self).__init__(name=name,
                                        pool=pool,
                                        vid=vid,
                                        size=size,
                                        **kwargs)
        self.target_dir = target_dir

    @property
    def usage(self):
        ''' Returns the actualy used space '''
        return get_disk_usage(self.vid)



class ReadWriteFile(SizeMixIn):
    # :pylint: disable=missing-docstring
    def __init__(self, **kwargs):
        super(ReadWriteFile, self).__init__(**kwargs)
        self.path = os.path.join(self.target_dir, self.name + '.img')
        self.vid = self.path


class ReadOnlyFile(Volume):
    # :pylint: disable=missing-docstring
    usage = 0

    def __init__(self, name=None, pool=None, vid=None, target_dir=None,
                 size=0, **kwargs):
        # :pylint: disable=unused-argument
        assert os.path.exists(vid), "read-only volume missing vid"
        super(ReadOnlyFile, self).__init__(name=name,
                                           pool=pool,
                                           vid=vid,
                                           size=size,
                                           **kwargs)
        self.path = self.vid


class OriginFile(SizeMixIn):
    # :pylint: disable=missing-docstring
    script = 'block-origin'

    def __init__(self, **kwargs):
        super(OriginFile, self).__init__(**kwargs)
        self.path_origin = os.path.join(self.target_dir, self.name + '.img')
        self.path_cow = os.path.join(self.target_dir, self.name + '-cow.img')
        self.path = '%s:%s' % (self.path_origin, self.path_cow)
        self.vid = self.path_origin

    def commit(self):
        raise NotImplementedError

    @property
    def usage(self):
        result = 0
        if os.path.exists(self.path_origin):
            result += get_disk_usage(self.path_origin)
        if os.path.exists(self.path_cow):
            result += get_disk_usage(self.path_cow)
        return result


class SnapshotFile(Volume):
    # :pylint: disable=missing-docstring
    script = 'block-snapshot'
    rw = False
    usage = 0

    def __init__(self, name=None, pool=None, vid=None, target_dir=None,
                 size=None, **kwargs):
        assert size
        super(SnapshotFile, self).__init__(name=name,
                                           pool=pool,
                                           vid=vid,
                                           size=size,
                                           **kwargs)
        self.path_origin = os.path.join(target_dir, name + '.img')
        self.path_cow = os.path.join(target_dir, name + '-cow.img')
        self.path = '%s:%s' % (self.path_origin, self.path_cow)
        self.vid = self.path_origin

    @property
    def created(self):
        return os.path.exists(self.path_origin) and os.path.exists(
            self.path_cow)


class VolatileFile(SizeMixIn):
    # :pylint: disable=missing-docstring

    def __init__(self, **kwargs):
        super(VolatileFile, self).__init__(**kwargs)
        self.path = os.path.join(self.target_dir, 'volatile.img')
        self.vid = self.path


def create_sparse_file(path, size):
    ''' Create an empty sparse file '''
    if os.path.exists(path):
        raise IOError("Volume %s already exists", path)
    parent_dir = os.path.dirname(path)
    if not os.path.exists(parent_dir):
        os.makedirs(parent_dir)
    with open(path, 'a+b') as fh:
        fh.truncate(size)


def get_disk_usage_one(st):
    '''Extract disk usage of one inode from its stat_result struct.

    If known, get real disk usage, as written to device by filesystem, not
    logical file size. Those values may be different for sparse files.

    :param os.stat_result st: stat result
    :returns: disk usage
    '''
    try:
        return st.st_blocks * BLKSIZE
    except AttributeError:
        return st.st_size


def get_disk_usage(path):
    '''Get real disk usage of given path (file or directory).

    When *path* points to directory, then it is evaluated recursively.

    This function tries estiate real disk usage. See documentation of
    :py:func:`get_disk_usage_one`.

    :param str path: path to evaluate
    :returns: disk usage
    '''
    try:
        st = os.lstat(path)
    except OSError:
        return 0

    ret = get_disk_usage_one(st)

    # if path is not a directory, this is skipped
    for dirpath, dirnames, filenames in os.walk(path):
        for name in dirnames + filenames:
            ret += get_disk_usage_one(os.lstat(os.path.join(dirpath, name)))

    return ret


def create_dir_if_not_exists(path):
    """ Check if a directory exists in if not create it.

        This method does not create any parent directories.
    """
    if not os.path.exists(path):
        os.mkdir(path)


def copy_file(source, destination):
    '''Effective file copy, preserving sparse files etc.
    '''
    # TODO: Windows support
    # We prefer to use Linux's cp, because it nicely handles sparse files
    assert os.path.exists(source), \
        "Missing the source %s to copy from" % source
    assert not os.path.exists(destination), \
        "Destination %s already exists" % destination

    parent_dir = os.path.dirname(destination)
    if not os.path.exists(parent_dir):
        os.makedirs(parent_dir)

    try:
        subprocess.check_call(['cp', '--reflink=auto', source, destination])
    except subprocess.CalledProcessError:
        raise IOError('Error while copying {!r} to {!r}'.format(source,
                                                                destination))


def _remove_if_exists(volume):
    if os.path.exists(volume.path):
        os.remove(volume.path)


def _check_path(path):
    ''' Raise an StoragePoolException if ``path`` does not exist'''
    if not os.path.exists(path):
        raise StoragePoolException('Missing image file: %s' % path)
