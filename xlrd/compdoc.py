# -*- coding: cp1252 -*-

##
# Implements the mimimal functionality required
# to extract a "Workbook" or "Book" stream (as one big string)
# from an OLE2 Compound Document file.
# <p>Copyright � 2005 John Machin, Lingfo Pty Ltd</p>
# <p>This module is part of the xlrd package, which is released under a BSD-style licence.</p>
##

# No part of the content of this file was derived from the works of David Giffin.

import sys
from struct import unpack

##
# Magic cookie that should appear in the first 8 bytes of the file.
SIGNATURE = "\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1"

EOCSID = -2
FREESID = -1
SATSID = -3
MSATSID = -4

class CompDocError(Exception):
    pass

class DirNode(object):

    def __init__(self, DID, dent, DEBUG=0):
        # dent is the 128-byte directory entry
        self.DID = DID
        (cbufsize, self.etype, self.colour, self.left_DID, self.right_DID,
        self.root_DID,
        self.first_SID,
        self.tot_size) = \
            unpack('<HBBiii16x4x8x8xii4x', dent[64:128])
        if cbufsize == 0:
            self.name = u''
        else:
            self.name = dent[0:cbufsize-2].decode('utf-16le') # omit the trailing U+0000
        self.children = [] # filled in later
        self.parent = -1 # indicates orphan; fixed up later
        self.tsinfo = unpack('<IIII', dent[100:116])
        if DEBUG:
            self.dump(DEBUG)

    def dump(self, DEBUG=1):
        print "DID=%d name=%r etype=%d DIDs(left=%d right=%d root=%d parent=%d kids=%r) first_SID=%d tot_size=%d" \
            % (self.DID, self.name, self.etype, self.left_DID,
            self.right_DID, self.root_DID, self.parent, self.children, self.first_SID, self.tot_size)
        if DEBUG == 2:
            # cre_lo, cre_hi, mod_lo, mod_hi = tsinfo
            print "timestamp info", self.tsinfo

def _build_family_tree(dirlist, parent_DID, child_DID):
    if child_DID < 0: return
    _build_family_tree(dirlist, parent_DID, dirlist[child_DID].left_DID)
    dirlist[parent_DID].children.append(child_DID)
    dirlist[child_DID].parent = parent_DID
    _build_family_tree(dirlist, parent_DID, dirlist[child_DID].right_DID)
    if dirlist[child_DID].etype == 1: # storage
        _build_family_tree(dirlist, child_DID, dirlist[child_DID].root_DID)

##
# Compound document handler.
# @param mem The raw contents of the file, as a string, or as a mmap.mmap() object. The
# only operation it needs to support is slicing.

class CompDoc(object):

    def __init__(self, mem, DEBUG=0):
        if mem[0:8] != SIGNATURE:
            raise CompDocError('Not an OLE2 compound document')
        if mem[28:30] != '\xFE\xFF':
            raise CompDocError('Expected "little-endian" marker, found %r' % mem[28:30])
        revision, version = unpack('<HH', mem[24:28])
        if DEBUG:
            print "\nCompDoc format: version=0x%04x revision=0x%04x" % (version, revision)
        self.mem = mem
        ssz, sssz = unpack('<HH', mem[30:34])
        self.sec_size = sec_size = 1 << ssz
        self.short_sec_size = 1 << sssz
        (
            SAT_tot_secs, self.dir_first_sec_sid, self.min_size_std_stream,
            SSAT_first_sec_sid, SSAT_tot_secs,
            MSAT_first_sec_sid, MSAT_tot_secs,
        ) = unpack('<ii4xiiiii', mem[44:76])

        if DEBUG:
            print 'sec sizes', ssz, sssz, sec_size, self.short_sec_size
            print "SAT_tot_secs=%d, dir_first_sec_sid=%d, min_size_std_stream=%d" \
                % (SAT_tot_secs, self.dir_first_sec_sid, self.min_size_std_stream,)
            print "SSAT_first_sec_sid=%d, SSAT_tot_secs=%d" % (SSAT_first_sec_sid, SSAT_tot_secs,)
            print "MSAT_first_sec_sid=%d, MSAT_tot_secs=%d" % (MSAT_first_sec_sid, MSAT_tot_secs,)
        nent = sec_size // 4 # number of SID entries in a sector
        fmt = "<%di" % nent
        #
        # === build the MSAT ===
        #
        MSAT = list(unpack('<109i', mem[76:512]))
        sid = MSAT_first_sec_sid
        while sid >= 0:
            offset = 512 + sec_size * sid
            news = list(unpack(fmt, mem[offset:offset+sec_size]))
            sid = news.pop()
            MSAT.extend(news)
        #
        # === build the SAT ===
        #
        self.SAT = []
        for msid in MSAT:
            if msid == FREESID: continue
            offset = 512 + sec_size * msid
            news = list(unpack(fmt, mem[offset:offset+sec_size]))
            self.SAT.extend(news)
        # print "SAT", self.SAT
        # for i, s in enumerate(self.SAT):
        #     print "entry: %4d offset: %6d, next entry: %4d" % (i, 512 + sec_size * i, s)

        # === build the directory ===
        #
        dbytes = self._get_stream(self.mem, 512, self.SAT, self.sec_size, self.dir_first_sec_sid)
        dirlist = []
        did = -1
        for pos in xrange(0, len(dbytes), 128):
            did += 1
            dirlist.append(DirNode(did, dbytes[pos:pos+128], 0))
        self.dirlist = dirlist
        _build_family_tree(dirlist, 0, dirlist[0].root_DID) # and stand well back ...
        if DEBUG:
            for d in dirlist:
                d.dump(DEBUG)
        #
        # === get the SSCS ===
        #
        sscs_dir = self.dirlist[0]
        assert sscs_dir.etype == 5 # root entry
        self.SSCS = self._get_stream(self.mem, 512, self.SAT, sec_size, sscs_dir.first_SID, sscs_dir.tot_size)
        # if DEBUG: print "SSCS", repr(self.SSCS)
        #
        # === build the SSAT ===
        #
        self.SSAT = []
        sid = SSAT_first_sec_sid
        while sid >= 0:
            start_pos = 512 + sid * sec_size
            news = list(unpack(fmt, mem[start_pos:start_pos+sec_size]))
            self.SSAT.extend(news)
            sid = self.SAT[sid]
        assert sid == EOCSID
        # if DEBUG: print "SSAT", self.SSAT

    def _get_stream(self, mem, base, sat, sec_size, start_sid, size=None):
        # print "_get_stream", base, sec_size, start_sid, size
        sectors = []
        s = start_sid
        if size is None:
            # nothing to check against
            while s >= 0:
                start_pos = base + s * sec_size
                sectors.append(mem[start_pos:start_pos+sec_size])
                s = sat[s]
            assert s == EOCSID
        else:
            todo = size
            while s >= 0:
                start_pos = base + s * sec_size
                grab = sec_size
                if grab > todo:
                    grab = todo
                todo -= grab
                sectors.append(mem[start_pos:start_pos+grab])
                s = sat[s]
            assert s == EOCSID
            assert todo == 0
        return ''.join(sectors)

    def _dir_search(self, path, storage_DID=0):
        # Return matching DirNode instance, or None
        head = path[0]
        tail = path[1:]
        dl = self.dirlist
        for child in dl[storage_DID].children:
            if dl[child].name == head:
                et = dl[child].etype
                if et == 2:
                    return dl[child]
                if et == 1:
                    if not tail:
                        raise CompDocError("Requested component is a 'storage'")
                    return self._dir_search(tail, child)
                dl[child].dump(1)
                raise CompDocError("Requested stream is not a 'user stream'")
        return None

    ##
    # Interrogate the compound document's directory; return the stream as a string if found, otherwise
    # return None.
    # @param qname Name of the desired stream e.g. u'Workbook'. Should be in Unicode or convertible thereto.

    def get_named_stream(self, qname):
        d = self._dir_search(qname.split("/"))
        if d is None:
            return None
        if d.tot_size >= self.min_size_std_stream:
            return self._get_stream(self.mem, 512, self.SAT, self.sec_size, d.first_SID, d.tot_size)
        else:
            return self._get_stream(self.SSCS, 0, self.SSAT, self.short_sec_size, d.first_SID, d.tot_size)

    ##
    # Interrogate the compound document's directory.
    # If the named stream is not found, (None, 0, 0) will be returned.
    # If the named stream is found and is contiguous within the original byte sequence ("mem")
    # used when the document was opened,
    # then (mem, offset_to_start_of_stream, length_of_stream) is returned.
    # Otherwise a new string is built from the fragments and (new_string, 0, length_of_stream) is returned.
    # @param qname Name of the desired stream e.g. u'Workbook'. Should be in Unicode or convertible thereto.

    def locate_named_stream(self, qname):
        d = self._dir_search(qname.split("/"))
        if d is None:
            return (None, 0, 0)
        if d.tot_size >= self.min_size_std_stream:
            return self._locate_stream(self.mem, 512, self.SAT, self.sec_size, d.first_SID, d.tot_size)
        else:
            return (
                self._get_stream(self.SSCS, 0, self.SSAT, self.short_sec_size, d.first_SID, d.tot_size),
                0,
                d.tot_size
                )
        return (None, 0, 0) # not found

    def _locate_stream(self, mem, base, sat, sec_size, start_sid, size):
        # print "_locate_stream", base, sec_size, start_sid, size
        s = start_sid
        if s < 0:
            raise CompDocError("_locate_stream: start_sid (%d) is -ve" % start_sid)
        p = -99 # dummy previous SID
        start_pos = -9999
        end_pos = -8888
        slices = []
        while s >= 0:
            if s == p+1:
                # contiguous sectors
                end_pos += sec_size
            else:
                # start new slice
                if p >= 0:
                    # not first time
                    slices.append((start_pos, end_pos))
                start_pos = base + s * sec_size
                end_pos = start_pos + sec_size
            p = s
            s = sat[s]
        assert s == EOCSID
        # print len(slices) + 1, "slices"
        if not slices:
            # The stream is contiguous ... just what we like!
            return (mem, start_pos, size)
        slices.append((start_pos, end_pos))
        return (''.join([mem[start_pos:end_pos] for start_pos, end_pos in slices]), 0, size)

# ==========================================================================================