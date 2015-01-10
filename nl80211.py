#!/usr/bin/python2.7

import fcntl
import os
import random
import socket
import struct


class Iterator(object):
  def __init__(self, data, offset=0, length=None):
    self.data = data
    self.offset = offset
    self.length = len(self.data) if length is None else length
    assert self.length <= len(self.data)

  def __str__(self):
    data = self.data[self.offset:self.length]
    return '(%d bytes): %r' % (len(data), data)

  def Advance(self, offset_incr):
    assert offset_incr <= self.Remaining(), 'Want %d bytes, have %d' % (offset_incr, self.Remaining())
    self.offset += offset_incr

  def Extract(self, length):
    assert length <= self.Remaining(), 'Want %d bytes, have %d' % (length, self.Remaining())
    ret = self.data[self.offset:self.offset + length]
    self.Advance(length)
    return ret

  def ExtractIterator(self, length):
    assert length <= self.Remaining(), 'Want %d bytes, have %d' % (length, self.Remaining())
    ret = Iterator(self.data, self.offset, self.offset + length)
    self.Advance(length)
    return ret

  def Remaining(self):
    return self.length - self.offset

  def AtEnd(self):
    return not self.Remaining()


class Accumulator(object):
  def __init__(self):
    self._parts = []

  def __str__(self):
    return ''.join(self._parts)

  def __len__(self):
    return sum(len(part) for part in self._parts)

  def Append(self, value):
    self._parts.append(value)


class SingleStructParser(struct.Struct):
  def Unpack(self, iterator):
    values = self.unpack_from(iterator.data, iterator.offset)
    iterator.Advance(self.size)
    assert len(values) == 1
    return values[0]

  def Pack(self, accumulator, value):
    accumulator.Append(self.pack(value))


class StructParser(struct.Struct):
  def __init__(self, format, fields=None):
    super(StructParser, self).__init__(format)
    self._fields = fields

  def Unpack(self, iterator):
    values = self.unpack_from(iterator.data, iterator.offset)
    iterator.Advance(self.size)
    return dict(zip(self._fields, values))

  def Pack(self, accumulator, **values):
    ordered_values = []
    for field in self._fields:
      ordered_values.append(values[field])
    accumulator.Append(self.pack(*ordered_values))


class StringParser(object):
  def Unpack(self, iterator):
    return iterator.Extract(iterator.Remaining())

  def Pack(self, accumulator, value):
    accumulator.Append(value)


class EmptyParser(object):
  def Unpack(self, iterator):
    return True

  def Pack(self, accumulator, value=None):
    pass


class Attribute(object):
  _nlattr = StructParser('HH', ('len', 'type'))

  def __init__(self, attributes):
   super(Attribute, self).__init__()
   self._attributes = attributes

  def Unpack(self, iterator):
    nlattr = self._nlattr.Unpack(iterator)
    value = iterator.data[iterator.offset:iterator.offset + nlattr['len'] - self._nlattr.size]
    name, sub_parser = self._attributes.get(nlattr['type'], (None, None))
    assert sub_parser, 'Unknown attribute type %d, len %d' % (nlattr['type'], nlattr['len'])
    sub_len = nlattr['len'] - self._nlattr.size
    sub_iterator = iterator.ExtractIterator(sub_len)
    ret = {
      name: sub_parser.Unpack(sub_iterator)
    }
    assert sub_iterator.AtEnd(), '%d bytes remaining' % sub_iterator.Remaining()

    padding = ((nlattr['len'] + 4 - 1) & ~3) - nlattr['len']
    iterator.Advance(padding)

    return ret

  def Pack(self, accumulator, attrtype, value):
    sub_parser = self._attributes[attrtype][1]
    sub_accumulator = Accumulator()
    sub_parser.Pack(sub_accumulator, value)
    attrlen = self._nlattr.size + len(sub_accumulator)
    self._nlattr.Pack(accumulator, len=attrlen, type=attrtype)
    accumulator.Append(str(sub_accumulator))

    padding = ((attrlen + 4 - 1) & ~3) - attrlen
    if padding:
      accumulator.Append('\0' * padding)


class Attributes(object):
  def __init__(self, attributes):
    super(Attributes, self).__init__()
    self._attribute_idx = dict((v[0], k) for k, v in attributes.iteritems())
    self._attribute = Attribute(attributes)

  def Unpack(self, iterator):
    ret = {}
    while not iterator.AtEnd():
      ret.update(self._attribute.Unpack(iterator))
    return ret

  def Pack(self, accumulator, **attrs):
    for name, value in attrs.iteritems():
      self._attribute.Pack(accumulator, self._attribute_idx[name], value)


class Array(object):
  _arrayhdr = StructParser('HH', ('len', 'index'))

  def __init__(self, child):
    super(Array, self).__init__()
    self._child = child

  def Unpack(self, iterator):
    ret = []
    while not iterator.AtEnd():
      hdr = self._arrayhdr.Unpack(iterator)
      sub_len = hdr['len'] - self._arrayhdr.size
      sub_iterator = iterator.ExtractIterator(sub_len)
      ret.append(self._child.Unpack(sub_iterator))
      assert sub_iterator.AtEnd(), '%d bytes remaining' % sub_iterator.Remaining()
    return ret


flag = EmptyParser()
string = StringParser()
u8 = SingleStructParser('B')
u16 = SingleStructParser('H')
u32 = SingleStructParser('L')
u64 = SingleStructParser('Q')


class Netlink(object):
  _NLMSG_F_REQUEST = 0x01
  _NLMSG_F_MULTI = 0x02
  _NLMSG_F_ACK = 0x04
  _NLMSG_F_ECHO = 0x08
  _NLMSG_F_DUMP_INTR = 0x10

  flags = {
    'root': 0x100,
    'match': 0x200,
    'atomic': 0x400,
    'dump': 0x100 | 0x200,
  }

  _NLMSG_DONE = 3

  _nlmsghdr = StructParser('LHHLL', ('length', 'type', 'flags', 'seq', 'pid'))

  def __init__(self):
    self._sock = socket.socket(socket.AF_NETLINK, socket.SOCK_DGRAM, 16)
    self._sock.bind((0, 0))

  def Send(self, msgtype, flags, msg):
    flagint = self._NLMSG_F_REQUEST
    for flag in flags:
      flagint |= self.flags[flag]
    accumulator = Accumulator()
    self._nlmsghdr.Pack(
      accumulator,
      length=len(msg) + self._nlmsghdr.size,
      type=msgtype,
      flags=flagint,
      seq=random.randint(0, 2 ** 32 - 1),
      pid=os.getpid())
    accumulator.Append(msg)
    self._sock.send(str(accumulator))

  def Recv(self):
    while True:
      data = self._sock.recv(4096)
      iterator = Iterator(data)
      while not iterator.AtEnd():
        myhdr = self._nlmsghdr.Unpack(iterator)
        if myhdr['type'] == self._NLMSG_DONE:
          return
        yield (myhdr['type'], iterator.ExtractIterator(myhdr['length'] - self._nlmsghdr.size))
        if not myhdr['flags'] & self._NLMSG_F_MULTI:
          return


class GenericNetlink(object):
  _genlmsghdr = StructParser('BBH', ('cmd', 'version', 'reserved'))

  _op_attr = Attributes({
    1: ('id', u32),
    2: ('flags', u32),
  })

  _mcast_grp_attr = Attributes({
    1: ('name', string),
    2: ('id', u32),
  })

  _ctrl_attr = Attributes({
    1: ('family_id', u16),
    2: ('family_name', string),
    3: ('version', u32),
    4: ('hdrsize', u32),
    5: ('maxattr', u32),
    6: ('ops', Array(_op_attr)),
    7: ('mcast_groups', Array(_mcast_grp_attr)),
  })

  def __init__(self):
    self._msgtypes = [
      {
        'id': 0x10,
        'name': 'nlctrl',
        'parser': self._ctrl_attr,
        'commands': {
          'newfamily': 1,
          'getfamily': 3,
        },
      },
    ]

    self._netlink = Netlink()
    self._UpdateMsgTypes()
    self.Send('nlctrl', ['dump'], 'getfamily', 1)
    for msg in self.Recv():
      msgtype, attrs = msg
      assert msgtype == self._msgtypes_by_name['nlctrl']['commands']['newfamily'], msgtype
      family_name = attrs['family_name'].rstrip('\0')
      if family_name in self._msgtypes_by_name:
        assert attrs['family_id'] == self._msgtypes_by_name[family_name]['id'], attrs['family_id']
      else:
        self._msgtypes.append({
          'id': attrs['family_id'],
          'name': family_name,
          'parser': None,
          'commands': None,
        })
    self._UpdateMsgTypes()

  def _UpdateMsgTypes(self):
    self._msgtypes_by_id = dict((i['id'], i) for i in self._msgtypes)
    self._msgtypes_by_name = dict((i['name'], i) for i in self._msgtypes)

  def RegisterMsgType(self, family_name, parser, commands):
    self._msgtypes_by_name[family_name]['parser'] = parser
    self._msgtypes_by_name[family_name]['commands'] = commands

  def Send(self, msgtype, flags, cmd, version, **attrs):
    msgtype = self._msgtypes_by_name[msgtype]

    accumulator = Accumulator()
    self._genlmsghdr.Pack(
      accumulator,
      cmd=msgtype['commands'][cmd],
      version=version,
      reserved=0)

    msgtype['parser'].Pack(
      accumulator,
      **attrs)

    self._netlink.Send(msgtype['id'], flags, str(accumulator))

  def Recv(self):
    for msgtype, iterator in self._netlink.Recv():
      genlhdr = self._genlmsghdr.Unpack(iterator)
      parser = self._msgtypes_by_id[msgtype]['parser']
      yield (genlhdr['cmd'], parser.Unpack(iterator))

  def Query(self, msgtype, flags, cmd, version, **attrs):
    self.Send(msgtype, flags, cmd, version, **attrs)
    return self.Recv()


def RegisterNL80211(gnl):
  rate_info = Attributes({
    1: ('bitrate', u16),
    2: ('mcs', u8),
    4: ('short_gi', flag),
    5: ('bitrate32', u32),
    9: ('80p80_mhz_width', u32),
    10: ('160_mhz_width', u32),
  })

  bss_param = Attributes({
    2: ('short_preamble', flag),
    3: ('short_slot_time', flag),
    4: ('dtim_period', u8),
    5: ('beacon_interval', u16),
  })

  sta_info = Attributes({
    1: ('inactive_time', u32),
    2: ('rx_bytes', u32),
    3: ('tx_bytes', u32),
    7: ('signal', u8),
    8: ('tx_bitrate', rate_info),
    9: ('rx_packets', u32),
    10: ('tx_packets', u32),
    11: ('tx_retries', u32),
    12: ('tx_failed', u32),
    13: ('signal_avg', u8),
    14: ('rx_bitrate', rate_info),
    15: ('bss_param', bss_param),
    16: ('connected_time', u32),
    17: ('sta_flags', StructParser('LL', ('mask', 'values'))),
    18: ('beacon_loss', u32),
    23: ('rx_bytes_64', u64),
    24: ('tx_bytes_64', u64),
  })

  nl80211_attr = Attributes({
    3: ('ifindex', u32),
    6: ('mac', string),
    21: ('sta_info', sta_info),
    46: ('generation', u32),
  })

  commands = {
    'get_station': 17,
  }

  # STA_FLAG_AUTHORIZED = 1 << 0
  # STA_FLAG_SHORT_PREAMBLE = 1 << 1
  # STA_FLAG_WME = 1 << 2
  # STA_FLAG_MFP = 1 << 3
  # STA_FLAG_AUTHENTICATED = 1 << 4
  # STA_FLAG_TDLS_PEER = 1 << 5
  # STA_FLAG_ASSOCIATED = 1 << 6

  gnl.RegisterMsgType('nl80211', nl80211_attr, commands)


def GetIfIndex(if_name):
  SIOCGIFINDEX = 0x8933
  sockfd = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
  ifreq = struct.pack('16si', if_name, 0)
  res = fcntl.ioctl(sockfd, SIOCGIFINDEX, ifreq)
  return struct.unpack("16si", res)[1]


gnl = GenericNetlink()
RegisterNL80211(gnl)
print list(gnl.Query('nl80211', ['dump'], 'get_station', 0, ifindex=GetIfIndex('wlan0')))
