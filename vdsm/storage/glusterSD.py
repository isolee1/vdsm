#
# Copyright 2012-2016 Red Hat, Inc.
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
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301 USA
#
# Refer to the README and COPYING files for full details of the license
#

import os
import nfsSD
import sd
import glusterVolume
import fileSD
import mount
import storage_exception as se


class GlusterStorageDomain(nfsSD.NfsStorageDomain):

    @classmethod
    def getMountPoint(cls, mountPath):
        return os.path.join(cls.storage_repository,
                            sd.DOMAIN_MNT_POINT, sd.GLUSTERSD_DIR, mountPath)

    def getVolumeClass(self):
        return glusterVolume.GlusterVolume

    @staticmethod
    def findDomainPath(sdUUID):
        glusterDomPath = os.path.join(sd.GLUSTERSD_DIR, "*")
        for tmpSdUUID, domainPath in fileSD.scanDomains(glusterDomPath):
            if tmpSdUUID == sdUUID and mount.isMounted(os.path.join(domainPath,
                                                       "..")):
                return domainPath

        raise se.StorageDomainDoesNotExist(sdUUID)


def findDomain(sdUUID):
    return GlusterStorageDomain(GlusterStorageDomain.findDomainPath(sdUUID))
