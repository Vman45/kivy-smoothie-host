import threading
import asyncio
import serial_asyncio
import aiofiles
import logging
import functools
import sys
import re
import traceback
import serial.tools.list_ports
import subprocess
import socket

async_main_loop= None

class SerialConnection(asyncio.Protocol):
    def __init__(self, cb, f, is_net= False):
        super().__init__()
        self.log = logging.getLogger() #.getChild('SerialConnection')
        self.log.debug('SerialConnection: creating SerialConnection')
        self.cb = cb
        self.f= f
        self.cnt= 0
        self.flush= False
        self.is_net= is_net
        self._paused = False
        self._drain_waiter = None
        self._connection_lost = False
        self.transport= None

    def connection_made(self, transport):
        self.transport = transport
        self.log.debug('SerialConnection: port opened: {}'.format(transport))
        if self.is_net:
            # we don't want to buffer the entire file on the host
            transport.get_extra_info('socket').setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 2048)
            self.log.info("SerialConnection: Setting net tx buf to 2048")
            # for net we want to limit how much we queue up otherwise the whole file gets queued
            # this also gives us more progress more often
            transport.set_write_buffer_limits(high=1024, low=256)
            self.log.info('Buffer limits: {}'.format(transport.get_write_buffer_limits()))
        else:
            #transport.serial.rts = False  # You can manipulate Serial object via transport
            pass

    def flush_queue(self):
        # if self.transport:
        #     self.transport.abort()
        # TODO does not do anything at the moment possible is to do transport.abort() but that closes the connection
        self.flush= True

    def send_message(self, data, hipri=False):
        """ Feed a message to the sender coroutine. """
        self.log.debug('SerialConnection: send_message: {}'.format(data))
        self.transport.write(data.encode('utf-8'))

    def data_received(self, data):
        #print('data received', repr(data))
        str= ''
        try:
            # FIXME this is a problem when it splits utf-8, may need to get whole lines here anyway
            str= data.decode('utf-8')
        except Exception as err:
            self.log.error("SerialConnection: Got decode error on data {}: {}".format(repr(data), err))
            str= repr(data) # send it upstream anyway

        self.cb.incoming_data(str)

    def connection_lost(self, exc):
        self.log.debug('SerialConnection: port closed')
        self._connection_lost = True

        # Wake up the writer if currently paused.
        if self._paused:
            waiter = self._drain_waiter
            if waiter:
                self._drain_waiter = None
                if not waiter.done():
                    if exc is None:
                        waiter.set_result(None)
                    else:
                        waiter.set_exception(exc)

        self.transport.close()
        self.f.set_result('Disconnected')

    @asyncio.coroutine
    def _drain_helper(self):
        if self._connection_lost:
            raise ConnectionResetError('Connection lost')
        if not self._paused:
            return
        waiter = self._drain_waiter
        assert waiter is None or waiter.cancelled()
        waiter = asyncio.Future()
        self._drain_waiter = waiter
        yield from waiter

    def pause_writing(self):
        self.log.debug('SerialConnection: pause writing: {}'.format(self.transport.get_write_buffer_size()))
        if not self.is_net:
            return
        # we only do this pause stream stuff for net
        assert not self._paused
        self._paused = True

    def resume_writing(self):
        self.log.debug('SerialConnection: resume writing: {}'.format(self.transport.get_write_buffer_size()))
        if not self.is_net:
            return
        # we only do this pause stream stuff for net
        assert self._paused
        self._paused = False
        waiter = self._drain_waiter
        if waiter is not None:
            self._drain_waiter = None
            if not waiter.done():
                waiter.set_result(None)

class Comms():
    def __init__(self, app, reportrate=1):
        self.app = app
        self.proto = None
        self.timer= None
        self._fragment= None
        self.abort_stream= False
        self.pause_stream= False #asyncio.Event()
        self.okcnt= None
        self.ping_pong= True # ping pong protocol for streaming
        self.file_streamer= None
        self.report_rate= reportrate
        self._reroute_incoming_data_to= None
        self.is_streaming= False
        self.do_query= False

        self.log = logging.getLogger() #.getChild('Comms')
        #logging.getLogger().setLevel(logging.DEBUG)

    def connect(self, port):
        ''' called from UI to connect to given port, runs the asyncio mainloop in a separate thread '''
        self.port= port
        self.log.info('Comms: creating comms thread')
        t= threading.Thread(target=self.run_async_loop)
        t.start()
        return t

    def disconnect(self):
        ''' called by ui thread to disconnect '''
        if self.proto:
            async_main_loop.call_soon_threadsafe(self.proto.transport.close)

    def write(self, data):
        ''' Write to serial port, called from UI thread '''
        if self.proto and async_main_loop:
            #self.log.debug('Comms: writing ' + data)
            async_main_loop.call_soon_threadsafe(self._write, data)
            #asyncio.run_coroutine_threadsafe(self.proto.send_message, async_main_loop)
        else:
            self.log.warning('Comms: Cannot write to closed connection: ' + data)
            #self.app.main_window.async_display("<<< {}".format(data))

    def _write(self, data):
        # calls the send_message in Serial Connection proto
        #self.log.debug('Comms: _write ' + data)
        if self.proto:
           self.proto.send_message(data)

    def _get_reports(self):
        if self.is_streaming:
            # when streaming we do not send the query here
            self.do_query= True;
        else:
            self.send_query()

    def send_query(self):
        # calls the send_message in Serial Connection proto
        if self.proto:
            if not self.app.is_cnc:
                self.proto.send_message('M105\n', True)
        self.proto.send_message('?' if not self.net_connection else 'get status\n', True)

    def stop(self):
        ''' called by ui thread when it is exiting '''
        if self.proto:
            # abort any streaming immediately
            self._stream_pause(False, True)
            if self.file_streamer:
                self.file_streamer.cancel()

            # we need to close the transport, this will cause mainloop to stop and thread to exit as well
            async_main_loop.call_soon_threadsafe(self.proto.transport.close)

        # else:
        #     if async_main_loop and async_main_loop.is_running():
        #         async_main_loop.call_soon_threadsafe(async_main_loop.stop)

    def get_ports(self):
        return [port for port in serial.tools.list_ports.comports() if port[2] != 'n/a']

    def run_async_loop(self):
        ''' called by connect in a new thread to setup and start the asyncio loop '''
        global async_main_loop

        if async_main_loop:
            self.log.error("Comms: Already running cannot connect again")
            self.app.main_window.async_display('>>> Already running cannot connect again')
            return

        newloop = asyncio.new_event_loop()
        asyncio.set_event_loop(newloop)
        loop = asyncio.get_event_loop()
        async_main_loop = loop
        f = asyncio.Future()

        # if tcp connection port will be net://ipaddress[:port]
        # otherwise it will be serial:///dev/ttyACM0 or serial://COM2:
        if self.port.startswith('net://'):
            sc_factory = functools.partial(SerialConnection, cb=self, f= f, is_net= True) # uses partial so we can pass a parameter
            self.net_connection= True
            ip= self.port[6:]
            ip= ip.split(':')
            if len(ip) == 1:
                self.port= 23
            else:
                self.port= ip[1]

            self.ipaddress= ip[0]
            self.log.info('Connecting to Network at {} port {}'.format(self.ipaddress, self.port))
            serial_conn= loop.create_connection(sc_factory, self.ipaddress, self.port)
            self.ping_pong= False # do not use ping pong for network connections

        elif self.port.startswith('serial://'):
            sc_factory = functools.partial(SerialConnection, cb=self, f= f) # uses partial so we can pass a parameter
            self.net_connection= False
            self.port= self.port[9:]
            serial_conn = serial_asyncio.create_serial_connection(loop, sc_factory, self.port, baudrate=115200)

        else:
            loop.close()
            self.log.error('Not a valid connection port: {}'.format(self.port))
            self.app.main_window.async_display('>>> Connect failed: unknown connection type, use "serial://" or "net://"'.format(self.port))
            self.app.main_window.disconnected()
            loop.close()
            async_main_loop= None
            return

        try:
            transport, self.proto = loop.run_until_complete(serial_conn) # sets up connection returning transport and protocol handler
            self.log.debug('Comms: serial connection task completed')

            # this is when we are really setup and ready to go, notify upstream
            self.app.main_window.connected()

            if self.report_rate > 0:
                # issue a version command to get things started
                self._write('\nversion\n')
                # start a timer to get the reports
                self.timer = loop.call_later(self.report_rate, self._get_reports)

            # wait until we are disconnected
            self.log.debug('Comms: waiting until disconnection')
            loop.run_until_complete(f)

            # clean up and notify upstream we have been disconnected
            self.proto= None # no proto now
            self._stream_pause(False, True) # abort the stream if one is running
            if self.timer: # stop the timer if we have one
                self.timer.cancel()
                self.timer= None

            self.app.main_window.disconnected() # tell upstream we disconnected

            # we wait until all tasks are complete
            pending = asyncio.Task.all_tasks()
            self.log.debug('Comms: waiting for all tasks to complete: {}'.format(pending))
            loop.run_until_complete(asyncio.gather(*pending))
            #loop.run_forever()

        except asyncio.CancelledError:
            pass

        except Exception as err:
            #self.log.error('Comms: {}'.format(traceback.format_exc()))
            self.log.error("Comms: Got serial error opening port: {0}".format(err))
            self.app.main_window.async_display(">>> Connect failed: {0}".format(err))
            self.app.main_window.disconnected()

        finally:
            loop.close()
            async_main_loop= None
            self.log.info('Comms: comms thread Exiting...')

    def list_sdcard(self, done_cb):
        ''' Issue a ls /sd and send results back to done_cb '''
        self.log.debug('Comms: list_sdcard')
        if self.proto and async_main_loop:
            async_main_loop.call_soon_threadsafe(self._list_sdcard, done_cb)
        else:
            self.log.warning('Comms: Cannot list sd on a closed connection')
            return False

        return True

    def _list_sdcard(self, done_cb):
        asyncio.async(self._parse_sdcard_list(done_cb))

    @asyncio.coroutine
    def _parse_sdcard_list(self, done_cb):
        self.log.debug('Comms: _parse_sdcard_list')

        if self.timer:
            # temorarily turn off status timer so we don't get unexpected lines in our file list
            self.timer.cancel()
            self.timer= None
            restart_timer= True
        else:
            restart_timer= False

        # setup callback to receieve and parse listing data
        files= []
        f= asyncio.Future()
        self._reroute_incoming_data_to= lambda x: self._rcv_sdcard_line(x, files, f)

        # issue command
        self._write('M20\n')

        # wait for it to complete and get all the lines
        # add a long timeout in case it fails and we don't want to wait for ever
        try:
            yield from asyncio.wait_for(f, 10)

        except asyncio.TimeoutError:
            self.log.warning("Comms: Timeout waiting for sd card list")
            files= []

        # turn off rerouting
        self._reroute_incoming_data_to= None

        if restart_timer:
            self.timer = async_main_loop.call_later(self.report_rate, self._get_reports)

        # call upstream callback with results
        done_cb(files)

    def _rcv_sdcard_line(self, l, files, f):
        # accumulate the file list, called with each line recieved

        if l.startswith('Begin file list') or l == 'ok':
            # ignore these lines
            return

        if l.startswith('End file list'):
            # signal we are done (TODO should we wait for the ok?)
            f.set_result(None)

        else:
            # accumulate the incoming lines
            files.append(l)

    # Handle incoming data, see if it is a report and parse it otherwise just display it on the console log
    # Note the data could be a line fragment and we need to only process complete lines terminated with \n
    tempreading_exp = re.compile("(^T:| T:)")
    def incoming_data(self, data):
        ''' called by Serial connection when incoming data is received '''
        l= data.splitlines(1)
        self.log.debug('Comms: incoming_data: {}'.format(l))

        # process incoming data
        for s in l:
            if self._fragment:
                # handle line fragment
                s= ''.join( (self._fragment, s) )
                self._fragment= None

            if not s.endswith('\n'):
                # this is the last line and is a fragment
                self._fragment= s
                break

            s= s.rstrip() # strip off \n

            if len(s) == 0: continue

            # send the line to the requested destination for processing
            if self._reroute_incoming_data_to is not None:
                self._reroute_incoming_data_to(s)
                continue

            # process a complete line
            if "ok C:" in s:
                self.handle_position(s)

            elif "ok T:" in s or self.tempreading_exp.findall(s):
                self.handle_temperature(s)

            elif s.startswith('ok'):
                if self.ping_pong:
                    if self.okcnt:
                        self.okcnt.release()
                elif self.okcnt is not None:
                    self.okcnt += 1

            elif "!!" in s or "ALARM" in s or "ERROR" in s:
                self.handle_alarm(s)

            elif s.startswith('<'):
                try:
                    self.handle_status(s)
                except:
                    self.log.error("Comms: error parsing status")

            elif s.startswith('//'):
                # ignore comments but display them
                # handle // action:pause etc
                pos= s.find('action:')
                if pos >= 0:
                    act= s[pos+7:].strip() # extract action command
                    if act in 'pause':
                        self.app.main_window.async_display('>>> Smoothie requested Pause')
                        self._stream_pause(True, False)
                    elif act in 'resume':
                        self.app.main_window.async_display('>>> Smoothie requested Resume')
                        self._stream_pause(False, False)
                    elif act in 'disconnect':
                        self.app.main_window.async_display('>>> Smoothie requested Disconnect')
                        self.disconnect()
                    else:
                        self.log.warning('Comms: unknown action command: {}'.format(act))

                else:
                    self.app.main_window.async_display('{}'.format(s))

            else:
                self.app.main_window.async_display('{}'.format(s))

    # Handle parsing of temp readings (Lifted mostly from Pronterface)
    tempreport_exp = re.compile("([TB]\d*):([-+]?\d*\.?\d*)(?: ?\/)?([-+]?\d*\.?\d*)")
    def parse_temperature(self, s):
        matches = self.tempreport_exp.findall(s)
        return dict((m[0], (m[1], m[2])) for m in matches)

    def handle_temperature(self, s):
        # ok T:19.8 /0.0 @0 B:20.1 /0.0 @0
        hotend_setpoint= None
        bed_setpoint= None
        hotend_temp= None
        bed_temp= None

        try:
            temps = self.parse_temperature(s)
            if "T" in temps and temps["T"][0]:
                hotend_temp = float(temps["T"][0])

            if "T" in temps and temps["T"][1]:
                hotend_setpoint = float(temps["T"][1])

            bed_temp = float(temps["B"][0]) if "B" in temps and temps["B"][0] else None
            if "B" in temps and temps["B"][1]:
                bed_setpoint = float(temps["B"][1])

            self.log.debug('Comms: got temps hotend:{}, bed:{}, hotend_setpoint:{}, bed_setpoint:{}'.format(hotend_temp, bed_temp, hotend_setpoint, bed_setpoint))
            self.app.main_window.update_temps(hotend_temp, hotend_setpoint, bed_temp, bed_setpoint)

        except:
            self.log.error(traceback.format_exc())

    def handle_position(self, s):
        # ok C: X:0.0000 Y:0.0000 Z:0.0000
        l= s.split(' ')
        if len(l) >= 5:
            x= float(l[2][2:])
            y= float(l[3][2:])
            z= float(l[4][2:])
            self.log.debug('Comms: got pos: X {}, Y {} Z {}'.format(x, y, z))
            #self.app.main_window.update_position(x, y, z)

    def handle_status(self, s):
        # requires 'new_status_format true' in smoothie config
        #<Idle|MPos:68.9980,-49.9240,40.0000,12.3456|WPos:68.9980,-49.9240,40.0000|F:12345.12|S:1.2>
        s= s[1:-1] # strip off < .. >

        # split fields
        l= s.split('|')
        if len(l) < 3:
            self.log.warning('Comms: old status report - set new_status_format')
            self.app.main_window.update_status("ERROR", "set new_status_format true")
            return

        # strip off status
        status= l[0]

        # strip of rest into a dict of name: [values,...,]
        d= { a: [float(y) for y in b.split(',')] for a, b in [x.split(':') for x in l[1:]] }

        # d= {}
        # for x in l[1:]:
        #     m= x.split(':')
        #     if len(m) != 2:
        #         raise ValueError
        #     d[m[0]]= [float(y) for y in m[1].split(',')]

        self.log.debug('Comms: got status:{} - rest: {}'.format(status, d))

        self.app.main_window.update_status(status, d)

        # schedule next report
        self.timer = async_main_loop.call_later(self.report_rate, self._get_reports)


    def handle_alarm(self, s):
        ''' handle case where smoothie sends us !! or an error of some sort '''
        self.log.warning('Comms: got error: {}'.format(s))
        # abort any streaming immediately
        self._stream_pause(False, True)
        if self.proto:
            self.proto.flush_queue()

        # call upstream after we have allowed stream to stop
        async_main_loop.call_soon(self.app.main_window.alarm_state, s)

    def stream_gcode(self, fn, progress=None):
        ''' called from external thread to start streaming a file '''
        self.progress= progress
        if self.proto and async_main_loop:
            async_main_loop.call_soon_threadsafe(self._stream_file, fn)
        else:
            self.log.warning('Comms: Cannot print to a closed connection')

    def _stream_file(self, fn):
        self.file_streamer= asyncio.async(self.stream_file(fn))

    def stream_pause(self, pause, do_abort= False):
        ''' called from external thread to pause or kill in process streaming '''
        async_main_loop.call_soon_threadsafe(self._stream_pause, pause, do_abort)

    def _stream_pause(self, pause, do_abort):
        if self.file_streamer:
            if do_abort:
                self.pause_stream= False
                self.abort_stream= True # aborts stream
                if self.ping_pong and self.okcnt:
                    self.okcnt.release() # release it in case it is waiting for ok so it can abort
                self.log.info('Comms: Aborting Stream')

            elif pause:
                self.pause_stream= True #.clear() # pauses stream
                self.log.info('Comms: Pausing Stream')

            else:
                self.pause_stream= False #.set() # releases pause on stream
                self.log.info('Comms: Resuming Stream')

    @asyncio.coroutine
    def stream_file(self, fn):
        self.log.info('Comms: Streaming file {} to port'.format(fn))
        self.is_streaming= True;
        self.abort_stream= False
        self.pause_stream= False #.set() # start out not paused
        if self.ping_pong:
            self.okcnt= asyncio.Semaphore(1)
        else:
            self.okcnt= 0

        f= None
        success= False
        linecnt= 0
        try:
            f = yield from aiofiles.open(fn, mode='r')
            while True:
                #yield from self.pause_stream.wait() # wait for pause to be released
                # needed to do it this way as the Event did not seem to work it would pause but not unpause
                # TODO maybe use Future here to wait for unpause
                # create future when pause then yield from it here then delete it
                while self.pause_stream:
                   yield from asyncio.sleep(1)
                   if self.abort_stream:
                        break

                line = yield from f.readline()

                if not line:
                    # EOF
                    break

                if self.abort_stream:
                    break

                l= line.strip()
                if l.startswith(';') or len(l) == 0:
                    continue


                if self.abort_stream:
                    break

                # wait for ok from previous line
                # Note we interleave read from file with wait for ok
                if self.ping_pong and self.okcnt:
                    try:
                        yield from self.okcnt.acquire()
                    except:
                        self.log.debug('Comms: okcntr wait cancelled')
                        break

                if self.abort_stream:
                    break

                # handle inline gcode commands to host
                if l.startswith('(cmd '):
                    s= l[5:-1] # get rest of line except terminating )
                    if 'image' in s:
                        fn= s[6:]
                        self.app.main_window.change_image(fn)
                    elif 'text' in s:
                        t= s[5:]
                        self.app.main_window.async_display(t)
                    elif 'pause' in s:
                        self._stream_pause(True, False) # we need to pause the stream
                        t= s[6:]
                        self.app.main_window.pause_prompt(t)
                    elif 'alarm' in s:
                        self.app.sound_alarm()
                    else:
                        self.log.debug('Comms: unknown inline command: {}'.format(s))


                # send the line
                self._write(line)

                # when streaming we need to yield until the flow control is dealt with
                if self.proto._connection_lost:
                    # Yield to the event loop so connection_lost() may be
                    # called.  Without this, _drain_helper() would return
                    # immediately, and code that calls
                    #     write(...); yield from drain()
                    # in a loop would never call connection_lost(), so it
                    # would not see an error when the socket is closed.
                    yield

                # if the buffers are full then wait until we can send some more
                yield from self.proto._drain_helper()

                if self.abort_stream:
                    break

                linecnt += 1
                if self.progress and linecnt%10 == 0: # update every 10 lines
                    if self.ping_pong:
                        # number of lines sent
                        self.progress(linecnt)
                    else:
                        # number of lines ok'd
                        self.progress(self.okcnt)

                # send query if ready, don't query if in fast stream mode though
                if self.ping_pong and self.do_query:
                    self.do_query= False
                    self.send_query()
                    # if the buffers are full then wait until we can send some more
                    yield from self.proto._drain_helper()

            success= not self.abort_stream

        except Exception as err:
                self.log.error("Comms: Stream file exception: {}".format(err))

        finally:
            if f:
                yield from f.close()

            if success and not self.ping_pong:
                self.log.debug('Comms: Waiting for okcnt to catch up: {} vs {}'.format(self.okcnt, linecnt))
                # we have to wait for all lines to be ack'd
                while self.okcnt < linecnt:
                    if self.progress:
                        self.progress(self.okcnt)
                    if self.abort_stream:
                        success= False
                        break

                    yield from asyncio.sleep(1)

            self.file_streamer= None
            self.progress= None
            self.okcnt= None
            self.is_streaming= False

            # notify upstream that we are done
            self.app.main_window.stream_finished(success)

            self.log.info('Comms: Streaming complete: {}'.format(success))

        return success

    @staticmethod
    def file_len(fname):
        ''' use external process to quickly find total number of G/M lines in file '''
        # NOTE some laser raster formats have lines that start with X and no G/M
        # and some CAm programs just output X or Y lines
        # TODO if windows use a slow python method
        p = subprocess.Popen(['grep', '-c', "^[GMXY]", fname], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        result, err = p.communicate()
        if p.returncode != 0:
            raise IOError(err)
        return int(result.strip().split()[0])


if __name__ == "__main__":

    import datetime

    ''' a standalone streamer to test it with '''
    logging.basicConfig(format='%(levelname)s:%(message)s', level=logging.DEBUG)

    class CommsApp(object):
        """ Standalone app callbacks """
        def __init__(self):
            super(CommsApp, self).__init__()
            self.root= self
            self.log = logging.getLogger()
            self.start_event= threading.Event()
            self.end_event= threading.Event()
            self.is_connected= False
            self.ok= False
            self.main_window= self

        def connected(self):
            self.log.debug("CommsApp: Connected...")
            self.is_connected= True
            self.start_event.set()

        def disconnected(self):
            self.log.debug("CommsApp: Disconnected...")
            self.is_connected= False
            self.start_event.set()

        def async_display(self, data):
            print(data)

        def stream_finished(self, ok):
            self.log.debug('CommsApp: stream finished: {}'.format(ok))
            self.ok= ok
            self.end_event.set()

        def alarm_state(self, s):
            self.ok= False
            # in this case we do want to disconnect
            comms.proto.transport.close()


    if len(sys.argv) < 3:
        print("Usage: {} port file".format(sys.argv[0]));
        exit(0)

    app= CommsApp()
    comms= Comms(app, 0) # Don't start the report query timer when streaming
    if len(sys.argv) > 3:
        comms.ping_pong= False
        print('Fast Stream')

    try:
        nlines= Comms.file_len(sys.argv[2]) # get number of lines so we can do progress and ETA
        print('number of lines: {}'.format(nlines))
    except:
        print('Exception: {}'.format(traceback.format_exc()))
        nlines= None

    start= None
    def display_progress(n):
        global start, nlines
        if not start:
            start= datetime.datetime.now()
            print("Print started at: {}".format(start.strftime('%x %X')))

        if nlines:
            now=datetime.datetime.now()
            d= (now-start).seconds
            if n > 10 and d > 10:
                # we have to wait a bit to get reasonable estimates
                lps= n/d
                eta= (nlines-n)/lps
            else:
                eta= 0
            et= datetime.timedelta(seconds=int(eta))
            print("progress: {}/{} {:.1%} ETA {}".format(n, nlines, n/nlines, et))

    try:
        t= comms.connect(sys.argv[1])
        if app.start_event.wait(5): # wait for connected as it is in a separate thread
            if app.is_connected:

                comms.stream_gcode(sys.argv[2], progress=lambda x: display_progress(x))
                app.end_event.wait() # wait for streaming to complete

                print("File sent: {}".format('Ok' if app.ok else 'Failed'))
                now=datetime.datetime.now()
                print("Print ended at : {}".format(now.strftime('%x %X')))
                if start:
                    et= datetime.timedelta(seconds= int((now-start).seconds))
                    print("Elapsed time: {}".format(et))

            else:
                print("Error: Failed to connect")

        else:
            print("Error: Connection timed out")

    except KeyboardInterrupt:
        print("Interrupted")
        #comms._stream_pause(False, True)

    finally:
        # now stop the comms if it is connected or running
        comms.stop()
        t.join()
