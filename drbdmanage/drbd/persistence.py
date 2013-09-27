#!/usr/bin/python

__author__="raltnoeder"
__date__ ="$Sep 24, 2013 3:33:50 PM$"

from drbdmanage.storage.storagecore import MinorNr
from drbdmanage.drbd.drbdcore import *
from drbdmanage.exceptions import *
from drbdmanage.utils import *
import sys
import json


class GenericPersistence(object):
    _obj = None
    
    def __init__(self, obj):
        self._obj = obj
    
    def get_object(self):
        return self._obj
    
    def load_dict(self, serializable):
        properties = dict()
        for key in serializable:
            try:
                val = self._obj.__dict__[key]
                properties[key] = val
            except KeyError:
                pass
        return properties
    
    def serialize(self, properties):
        return json.dumps(properties, indent=4, sort_keys=True)


class PersistenceImpl(object):
    _file      = None
    _server    = None
    _writeable = False
    
    BLKSZ      = 0x1000 # 4096
    IDX_OFFSET = 0x0800 # 2048
    CONF_FILE  = "/tmp/drbdmanaged.bin"
    
    def __init__(self):
        pass
    
    def open(self):
        rc = False
        try:
            self._file      = open(self.CONF_FILE, "r")
            self._writeable = False
            rc = True
        except Exception:
            pass
        return rc
    
    def open_modify(self):
        rc = False
        try:
            self._file      = open(self.CONF_FILE, "r+")
            self._writeable = True
            rc = True
        except Exception:
            pass
        return rc
    
    # TODO: clean implementation - this is a prototype
    def save(self, nodes, volumes):
        rc = False
        try:
            if self._writeable:
                p_nodes_con = dict()
                p_vol_con   = dict()
                p_assg_con  = dict()
                
                # Prepare nodes container (and build assignments list)
                assignments = []
                for node in nodes.itervalues():
                    p_node = DrbdNodePersistence(node)
                    p_node.save(p_nodes_con)
                    for assg in node.iterate_assignments():
                        assignments.append(assg)
                
                # Prepare volumes container
                for volume in volumes.itervalues():
                    p_volume = DrbdVolumePersistence(volume)
                    p_volume.save(p_vol_con)
                
                # Prepare assignments container
                for assignment in assignments:
                    p_assignment = AssignmentPersistence(assignment)
                    p_assignment.save(p_assg_con)
                
                # Save data
                self._file.seek(self.BLKSZ)
                
                nodes_off = self._file.tell()
                safe_data = self._container_to_json(p_nodes_con)
                self._file.write(safe_data)
                nodes_len = self._file.tell() - nodes_off
                
                self._align_offset()
                
                vol_off = self._file.tell()
                safe_data = self._container_to_json(p_vol_con)
                self._file.write(safe_data)
                vol_len = self._file.tell() - vol_off
                
                self._align_offset()
                
                assg_off = self._file.tell()
                safe_data = self._container_to_json(p_assg_con)
                self._file.write(safe_data)
                assg_len = self._file.tell() - assg_off
                
                self._file.seek(self.IDX_OFFSET)
                self._file.write( \
                  long_to_bin(nodes_off) \
                  + long_to_bin(nodes_len) \
                  + long_to_bin(vol_off) \
                  + long_to_bin(vol_len) \
                  + long_to_bin(assg_off) \
                  + long_to_bin(assg_len))
                
                rc = True
        except Exception as exc:
            sys.stderr.write(str(exc) + "\n")
        return rc
    
    # TODO: clean implementation - this is a prototype
    def load(self, nodes, volumes):
        rc = False
        try:
            if self._file is not None:
                self._file.seek(self.IDX_OFFSET)
                index = self._file.read(48)
                nodes_off = long_from_bin(index[0:8])
                nodes_len = long_from_bin(index[8:16])
                vol_off   = long_from_bin(index[16:24])
                vol_len   = long_from_bin(index[24:32])
                assg_off  = long_from_bin(index[32:40])
                assg_len  = long_from_bin(index[40:48])
                
                # begin DEBUG
                sys.stderr.write("nodes@" + str(nodes_off) + "\n")
                sys.stderr.write("volumes@" + str(vol_off) + "\n")
                sys.stderr.write("assignments@" + str(assg_off) + "\n")
                # end DEBUG
                
                self._file.seek(nodes_off)
                load_data = self._file.read(nodes_len)
                nodes_con = self._json_to_container(load_data)
                
                self._file.seek(vol_off)
                load_data = self._file.read(vol_len)
                vol_con   = self._json_to_container(load_data)
                
                self._file.seek(assg_off)
                load_data = self._file.read(assg_len)
                assg_con  = self._json_to_container(load_data)
                
                for properties in nodes_con.itervalues():
                    node = DrbdNodePersistence.load(properties)
                    if node is not None:
                        nodes[node.get_name()] = node
                
                for properties in vol_con.itervalues():
                    volume = DrbdVolumePersistence.load(properties)
                    if volume is not None:
                        volumes[volume.get_name()] = volume
                
                for properties in assg_con.itervalues():
                    assignment = AssignmentPersistence.load(properties, \
                      nodes, volumes)
                
                rc = True
        except Exception as exc:
            sys.stderr.write(str(exc) + "\n")
        return rc
    
    def close(self):
        try:
            if self._file is not None:
                self._writeable = False
                self._file.close()
                self._file      = None
        except Exception:
            pass
    
    def _container_to_json(self, container):
        return (json.dumps(container, indent=4, sort_keys=True) + "\n")
    
    def _json_to_container(self, json_doc):
        return json.loads(json_doc)
    
    def _align_offset(self):
        if self._file is not None:
            offset = self._file.tell()
            if offset % self.BLKSZ != 0:
                offset = ((offset / self.BLKSZ) + 1) * self.BLKSZ
                self._file.seek(offset)
    
    def _next_json(self, stream):
        read = False
        json_blk = None
        cfgline = stream.readline()
        while len(cfgline) > 0:
            if cfgline == "{\n":
                read = True
            if read:
                if json_blk is None:
                    json_blk = ""
                json_blk += cfgline
            if cfgline == "}\n":
                break
            cfgline = stream.readline()
        if json_blk is not None:
            sys.stderr.write("DEBUG: json_blk:\n" + json_blk + "\n")
        else:
            sys.stderr.write("DEBUG: json_blk = None\n")
        return json_blk

class DrbdNodePersistence(GenericPersistence):
    SERIALIZABLE = [ "_name", "_ip", "_af", "_state", \
      "_poolsize", "_poolfree" ]
    
    def __init__(self, node):
        super(DrbdNodePersistence, self).__init__(node)
    
    def save(self, container):
        node = self.get_object()
        properties  = self.load_dict(self.SERIALIZABLE)
        container[node.get_name()] = properties
    
    @classmethod
    def load(cls, properties):
        node = None
        try:
            node = DrbdNode( \
              properties["_name"], \
              properties["_ip"], \
              properties["_af"] \
              )
            node.set_state(properties["_state"])
            node.set_poolsize(properties["_poolsize"])
            node.set_poolfree(properties["_poolfree"])
        except Exception:
            pass
        return node


class DrbdVolumePersistence(GenericPersistence):
    SERIALIZABLE = [ "_name", "_state", "_size_MiB" ]
    
    def __init__(self, volume):
        super(DrbdVolumePersistence, self).__init__(volume)
    
    def save(self, container):
        volume = self.get_object()
        properties  = self.load_dict(self.SERIALIZABLE)
        minor = volume.get_minor()
        properties["minor"] = minor.get_value()
        container[volume.get_name()] = properties
    
    @classmethod
    def load(cls, properties):
        volume = None
        try:
            minor_nr = properties["minor"]
            minor = MinorNr(minor_nr)
            volume = DrbdVolume( \
              properties["_name"], \
              properties["_size_MiB"], \
              minor
              )
            volume.set_state(properties["_state"])
        except Exception:
            pass
        return volume


class AssignmentPersistence(GenericPersistence):
    SERIALIZABLE = [ "_blockdevice", "bd_path", "_node_id", \
      "_cstate", "_tstate", "_rc" ]
    
    def __init__(self, assignment):
        super(AssignmentPersistence, self).__init__(assignment)
        
    def save(self, container):
        properties = self.load_dict(self.SERIALIZABLE)
        
        # Serialize the names of nodes and volumes only
        assignment  = self.get_object()
        node        = assignment.get_node()
        volume      = assignment.get_volume()
        node_name   = node.get_name()
        vol_name    = volume.get_name()
        
        properties["node"]        = node_name
        properties["volume"]      = vol_name
        
        assg_name = node_name + ":" + vol_name
        
        container[assg_name] = properties
    
    @classmethod
    def load(cls, properties, nodes, volumes):
        assignment = None
        try:
            node = nodes[properties["node"]]
            volume = volumes[properties["volume"]]
            assignment = Assignment( \
              node, \
              volume, \
              properties["_node_id"], \
              properties["_cstate"], \
              properties["_tstate"] \
              )
            blockdevice = None
            bd_path     = None
            try:
                blockdevice = properties["_blockdevice"]
                bd_path     = properties["_bd_path"]
            except KeyError:
                pass
            if blockdevice is not None and bd_path is not None:
                assignment.set_blockdevice(blockdevice, bd_path)
            assignment.set_rc(properties["_rc"])
            node.add_assignment(assignment)
            volume.add_assignment(assignment)
        except Exception as exc:
            sys.stderr.write(str(exc) + "\n")
        return assignment
