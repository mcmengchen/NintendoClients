
from nintendo.pia.common import StationAddress
from nintendo.pia.station import StationConnectionInfo
from nintendo.pia.transport import ReliableTransport
from nintendo.pia.packet import PIAMessage
from nintendo.common import signal
import collections

import logging
logger = logging.getLogger(__name__)


class StationList:
	def __init__(self):
		self.stations = [None] * 32
	
	def add(self, station, index=None):
		if index is None:
			index = self.next_index()
			
		if not self.is_usable(index):
			raise IndexError("Tried to assign station to occupied index")

		station.index = index
		self.stations[index] = station
		
	def is_usable(self, index):
		return self.stations[index] is None
		
	def next_index(self):
		if None not in self.stations:
			raise OverflowError("A mesh can only hold up to 32 stations at once")
		return self.stations.index(None)
		
	def __len__(self):
		return 32 - self.stations.count(None)
		
	def __getitem__(self, index):
		filtered = list(filter(None, self.stations))
		return filtered[index]

		
class StationInfo(collections.namedtuple("StationInfo", "connection_info index")):
	@classmethod
	def deserialize(cls, data):
		conn_info = StationConnectionInfo.deserialize(data)
		index = data[conn_info.sizeof()]
		return cls(conn_info, index)
		
	def serialize(self):
		return self.connection_info.serialize() + bytes([self.index, 0])
		
	@staticmethod
	def sizeof(): return StationConnectionInfo.sizeof() + 2

		
class JoinResponseDecoder:

	finished = signal.Signal()

	def __init__(self):
		self.reset()
		
	def reset(self):
		self.station = None
		
	def parse(self, station, message):
		if self.station is None:
			self.update_info(station, message)
		elif not self.check_info(station, message):
			logger.warning("Incompatible join response fragment received")
			self.reset()
			self.update_info(station, message)
			
		fragment_index = message[5]
		if not self.fragments_received[fragment_index]:
			self.fragments_received[fragment_index] = True
			
			fragment_length = message[6]
			fragment_offs = message[7]
			
			offset = 8
			for i in range(fragment_length):
				index = fragment_offs + i
				
				if self.infos[index]:
					logger.warning("Overlapping join response fragments received")
				
				info = StationInfo.deserialize(message[offset:])
				offset += StationInfo.sizeof()
				self.infos[index] = info
				
			if all(self.infos):
				self.finished(station, self.host_index, self.assigned_index, self.infos)
				self.reset()

	def update_info(self, station, message):
		self.station = station
		self.num_stations = message[1]
		self.host_index = message[2]
		self.assigned_index = message[3]
		self.num_fragments = message[4]
		self.fragments_received = [False] * self.num_fragments
		self.infos = [None] * self.num_stations
		
	def check_info(self, station, message):
		return self.station == station and \
		       self.num_stations == message[1] and \
			   self.host_index == message[2] and \
			   self.assigned_index == message[3] and \
			   self.num_fragments == message[4]


class MeshProtocol:

	PROTOCOL_ID = 0x200
	
	PORT_UNRELIABLE = 0
	PORT_RELIABLE = 1
	
	MESSAGE_JOIN_REQUEST = 0x1
	MESSAGE_JOIN_RESPONSE = 0x2
	MESSAGE_LEAVE_REQUEST = 0x4
	MESSAGE_LEAVE_RESPONSE = 0x8
	MESSAGE_DESTROY_MESH = 0x10
	MESSAGE_DESTROY_RESPONSE = 0x11
	MESSAGE_UPDATE_MESH = 0x20
	MESSAGE_KICKOUT_NOTICE = 0x21
	MESSAGE_DUMMY = 0x22
	MESSAGE_CONNECTION_FAILURE = 0x24
	MESSAGE_GREETING = 0x40
	MESSAGE_MIGRATION_FINISH = 0x41
	MESSAGE_GREETING_RESPONSE = 0x42
	MESSAGE_MIGRATION_START = 0x44
	MESSAGE_MIGRATION_RESPONSE = 0x48
	MESSAGE_MULTI_MIGRATION_START = 0x49
	MESSAGE_MULTI_MIGRATION_RANK_DECISION = 0x4A
	MESSAGE_CONNECTION_REPORT = 0x80
	MESSAGE_RELAY_ROUTE_DIRECTIONS = 0x81
	
	on_join_request = signal.Signal()
	on_join_response = signal.Signal()
	on_join_denied = signal.Signal()
	
	station_joined = signal.Signal()
	station_left = signal.Signal()

	def __init__(self, session):
		self.session = session
		self.transport = session.transport
		self.resender = session.resending_transport
		self.station_protocol = session.station_protocol
		
		self.handlers = {
			self.MESSAGE_JOIN_REQUEST: self.handle_join_request,
			self.MESSAGE_JOIN_RESPONSE: self.handle_join_response,
			self.MESSAGE_UPDATE_MESH: self.handle_update_mesh
		}
		
		self.sliding_windows = [None] * 32
		
		self.join_response_decoder = JoinResponseDecoder()
		self.join_response_decoder.finished.add(self.on_join_response)
		
	def assign_sliding_window(self, station):
		self.sliding_windows[station.index] = ReliableTransport(
			self.transport, station, self.PROTOCOL_ID,
			self.PORT_RELIABLE, self.handle_message
		)
	
	def handle(self, station, message):
		if message.protocol_port == self.PORT_UNRELIABLE:
			self.handle_message(station, message.payload)
		elif message.protocol_port == self.PORT_RELIABLE:
			if station.index == 0xFD:
				logger.warning("Received reliable mesh packet from unknown station")
			else:
				transport = self.sliding_windows[station.index]
				transport.handle(message)
		else:
			logger.warning("Unknown MeshProtocol port: %i", packet.protocol_port)
				
	def handle_message(self, station, message):
		message_type = message[0]
		self.handlers[message_type](station, message)

	def handle_join_request(self, station, message):
		logger.info("Received join request")
		station_address = StationAddress.deserialize(message[4:])
		station_index = message[1]
		self.station_protocol.send_ack(station, message)
		self.on_join_request(station, station_index, station_address)
		
	def handle_join_response(self, station, message):
		logger.info("Received join response")
		if message[1] == 0:
			self.on_join_denied(station, message[4])
		else:
			self.station_protocol.send_ack(station, message)
			self.join_response_decoder.parse(station, message)
			
	def handle_update_mesh(self, station, message):
		print("Mesh update")
			
	def send_join_request(self, station):
		logger.debug("Sending join request")
		
		data = bytes([
			self.MESSAGE_JOIN_REQUEST, self.session.station.index, 0, 0
		])
		data += self.session.station.station_address().serialize()

		self.send(station, data, 0, True)
		
	def send_join_response(self, station, assigned_index, host_index, stations):
		logger.debug("Sending join response")

		infosize = (StationConnectionInfo.sizeof() + 4) & ~3
		limit = self.transport.size_limit() - 0xC
		
		per_packet = limit // infosize
		fragments = (len(stations) + per_packet - 1) // per_packet
		
		for i in range(fragments):
			offset = i * per_packet
			remaining = len(stations) - offset
			num_infos = min(remaining, per_packet)
			
			data = bytes([
				self.MESSAGE_JOIN_RESPONSE, len(stations),
				host_index, assigned_index, fragments, i,
				num_infos, offset
			])

			for j in range(num_infos):
				station_info = stations[offset + j]
				data += station_info.connection_info.serialize()
				data += bytes([station_info.index, 0])
			self.send(station, data, 0, True)
		
	def send_deny_join(self, station, reason):
		logger.debug("Denying join request")
		data = bytes([self.MESSAGE_JOIN_RESPONSE, 0, 0xFF, 0xFF, reason])
		self.send(station, data, 0)
		self.send(station, data, 8)
		
	def send_update_mesh(self, counter, host_index, stations):
		logger.debug("Sending mesh update")
		
		data += struct.pack(
			">BBBBIBBBB", self.MESSAGE_UPDATE_MESH, len(stations),
			host_index, 0, counter, 1, 0, host_index, 0
		)
		for station in stations:
			data += station.connection_info.serialize()
			data += bytes([station.index, 0])
			
		for reliable_transport in filter(None, self.sliding_windows):
			reliable_transport.send(data)
		
	def send(self, station, payload, flags, ack=False):
		message = PIAMessage()
		message.flags = flags
		message.protocol_id = self.PROTOCOL_ID
		message.protocol_port = self.PORT_UNRELIABLE
		message.payload = payload
		if ack:
			self.resender.send(station, message)
		else:
			self.transport.send(station, message)

			
class MeshMgr:

	join_succeeded = signal.Signal()
	join_denied = signal.Signal()
	station_joined = signal.Signal()

	def __init__(self, session):
		self.session = session
		self.protocol = session.mesh_protocol
		self.protocol.on_join_request.add(self.handle_join_request)
		self.protocol.on_join_response.add(self.handle_join_response)
		self.protocol.on_join_denied.add(self.handle_join_denied)
		
		self.station_mgr = session.station_mgr
		self.station_mgr.station_connected.add(self.handle_station_connected)
		
		self.stations = StationList()
		self.host_index = None
		
		self.update_counter = -1
		
		self.expecting_join_response = False
		
		self.pending_connect = {}
		
	def is_host(self):
		return self.session.station.index == self.host_index
		
	def handle_join_request(self, station, station_index, station_address):
		if self.is_host():
			if station != self.station_mgr.find_by_address(station_addr.address):
				logger.warning("Received join request with unexpected station address")
				self.protocol.send_deny_join(station, 2)
			else:
				self.send_join_response(station)
				self.send_update_mesh()
				self.station_joined(station)
		else:
			logger.warning("Received join request even though we aren't host")
			self.protocol.send_deny_join(station, 1)

	def handle_join_response(self, station, host_index, my_index, infos):
		if not self.expecting_join_response:
			logger.warning("Unexpected join response received")
		else:
			self.expecting_join_response = False
			self.host_index = host_index
			self.stations.add(self.session.station, my_index)
			for info in infos:
				rvcid = info.connection_info.public_station.rvcid
				self.pending_connect[rvcid] = info.index
			self.join_succeeded(infos)
	
	def handle_station_connected(self, station):
		if station.rvcid in self.pending_connect:
			index = self.pending_connect.pop(station.rvcid)
			if self.stations.is_usable(index):
				self.stations.add(station, index)
				self.protocol.assign_sliding_window(station)
				self.station_joined(station)
			else:
				logger.warning("Tried to assign station to occupied index")
	
	def handle_join_denied(self, station, reason):
		self.join_denied(station)
	
	def send_join_response(self, station):
		index = self.stations.next_index()
		self.protocol.send_join_response(
			station, index, self.host_index, self.stations
		)
		self.stations.add(station)
		self.protocol.assign_sliding_window(station)
		
	def send_update_mesh(self):
		self.update_counter += 1
		self.protocol.send_update_mesh(
			self.update_counter, self.host_index, self.stations
		)
	
	def create(self):
		self.stations.add(self.session.station)
		self.host_index = self.session.station.index
	
	def join(self, host_station):
		self.expecting_join_response = True
		self.protocol.send_join_request(host_station)