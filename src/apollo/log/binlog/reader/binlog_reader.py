import time
from pymysqlreplication import BinLogStreamReader
from threading import Thread
from threading import Lock
from queue import Queue
from apollo.log.binlog.reader.binlog_stream_factory import BinLogStreamFactory
from apollo.log.binlog.reader.binlog_metadata import BinLogMetadata
from apollo.processors.cancellation import Cancellation
from apollo.processors.dynamic_throttling import DynamicThrottling
from apollo.log.binlog.reader.read_log_error import ReadLogError
from apollo.log.binlog.reader.binlog_filtering import BinLogFiltering
from apollo.monitoring.tracing import print_log_reader, handle_log_reader_exception
from apollo.monitoring.instrumentation import Instrumentation

class BinLogReader:

    _stream = None
    _reading_thread = None
    _log_metadata = None
    _cancellation = None #Cancelation
    _lock_object = None
    _throttling = None

    _start_position = 4
    _binlog_filtering = None #BinLogFiltering
    _instrumentation = None

    def __init__(self, metadata: BinLogMetadata, instrumentation: Instrumentation, throttling: DynamicThrottling):
        self._lock_object = Lock()
        self._throttling = throttling
        self._instrumentation = instrumentation

        self._log_metadata = metadata
        self._reading_thread = Thread(target=self._do_read_log)
        self._reading_thread.setDaemon(True)

    def start(self, binlog_filtering: BinLogFiltering, cancellation: Cancellation):
        self._binlog_filtering = binlog_filtering
        self._cancellation = cancellation
        self._reading_thread.start()

    def stop(self):
        self._lock_object.acquire()
        try:
            self._cancellation.cancel()
            if self._stream != None:
                self._stream.close()
        finally:
            self._lock_object.release()

    def _do_read_log(self):
        streamFactory = BinLogStreamFactory()
        while True and not self._cancellation.is_cancel:
            self._stream = streamFactory.factory(self._log_metadata)
            try:
                self._read_stream()
            except Exception as exp:
                handle_log_reader_exception(exp)
                self._throttling.penalize_read_error()
                time.sleep(1)
        
        print('stop log reader')
    
    def _read_stream(self):
        log_pos = self._log_metadata.log_pos if self._log_metadata != None else 0
        log_file = self._log_metadata.log_file if self._log_metadata != None else ''
        for binlogevent in self._stream:
            self._handle_event(binlogevent)
            log_pos = self._stream.log_pos
            log_file = self._stream.log_file
        
        self._stream.close()
        if log_pos > self._start_position:
            self._log_metadata = BinLogMetadata(log_pos=log_pos, log_file=log_file)

        time.sleep(1)
        if self._log_metadata != None:
            print_log_reader('End of stream, re-connect from {} {}'.format(log_pos, log_file))

    def _handle_event(self, binlogevent):
        for row in binlogevent.rows:
            log_pos = self._stream.log_pos
            log_file = self._stream.log_file
            if self._binlog_filtering.filter(binlogevent, row, log_pos, log_file):
                 self._instrumentation.filter()
                 self._throttling.wait_to_read_log(self._cancellation)
            self._instrumentation.read_log()

    def __del__(self):
        if self._stream != None:
            self._stream.close()