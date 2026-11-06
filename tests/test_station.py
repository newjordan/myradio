"""Deterministic playout tests plus real loopback HTTP and MCP boundary tests."""
import datetime as dt
import io
import json
import queue
import struct
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import wave
from pathlib import Path
from unittest.mock import patch

from myradio.chat import Chat, tools
from myradio.model_transport import ModelTransport
from myradio.mcp import Client, dispatch, serve
from myradio.server import Server, load_token
from myradio.station import FRAMES, SILENCE, Station, StationError


def audio(sample=1234, frames=FRAMES, channels=1, rate=44100):
    output = io.BytesIO()
    with wave.open(output, 'wb') as f:
        f.setparams((channels, 2, rate, 0, 'NONE', 'not compressed'))
        f.writeframes(struct.pack('<h', sample) * frames * channels)
    return output.getvalue()


class StationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.station = Station(self.temp.name, start_worker=False)

    def tearDown(self):
        self.station.close()
        self.temp.cleanup()

    def track(self, name='Track', sample=1234, kind='music', frames=FRAMES):
        return self.station.import_wav(audio(sample, frames), name, 'Artist', kind)

    def test_recurring_program_persistence_order_and_misses(self):
        station = self.station
        track = self.track('Daily ID', kind='speech')
        base = dt.datetime(2026, 1, 5, 9, tzinfo=dt.timezone.utc).timestamp()
        with patch('myradio.station.time.time', return_value=base):
            station.action('program.add', {'track_id':track['id'], 'timezone':'UTC',
                                         'local_time':'10:00', 'weekdays':'1,2,3,4,5,6,7'})
        program = station.status()['programs'][0]
        due = base + 3600
        self.assertEqual(program['next_due'], due)
        with self.assertRaises(StationError):
            station.action('library.delete', {'track_id':track['id']})
        station.close()
        self.station = station = Station(self.temp.name, start_worker=False)
        self.assertEqual(station.status()['programs'][0]['id'], program['id'])
        station.tick(now=due)  # off-air: mark missed and retain the rule
        self.assertEqual(station.status()['programs'][0]['last_status'], 'missed')
        self.assertEqual(station.status()['programs'][0]['next_due'], due + 86400)
        self.assertEqual(station.status()['queue'], [])
        station.action('transport.start')
        self.assertEqual(station.tick(now=due + 86400), struct.pack('<hh',1234,1234) * FRAMES)
        self.assertEqual(station.status()['programs'][0]['last_status'], 'queued')
        station.tick(now=due + 86400)  # identical clock: no duplicate airing
        self.assertEqual(station.status()['queue'], [])
        station.tick(now=due + 10 * 86400 + 120)  # no catch-up burst
        self.assertEqual(station.status()['programs'][0]['last_status'], 'missed')
        self.assertEqual(station.status()['queue'], [])
        self.assertGreater(station.status()['programs'][0]['next_due'], due + 10 * 86400 + 120)
        station.action('program.remove', {'program_id':program['id']})
        station.action('library.delete', {'track_id':track['id']})
        self.assertEqual(station.status()['programs'], [])

    def test_recurrence_dst_gaps_folds_and_invalid_rules(self):
        from myradio.scheduling import next_occurrence
        def epoch(iso):
            return dt.datetime.fromisoformat(iso).timestamp()
        # Spring-forward Sunday: nonexistent 02:30 is skipped, not shifted to 03:30.
        self.assertEqual(next_occurrence('America/New_York','02:30','7',epoch('2026-03-08T00:00:00-05:00')),
                         epoch('2026-03-15T02:30:00-04:00'))
        first_fold = epoch('2026-11-01T01:30:00-04:00')
        self.assertEqual(next_occurrence('America/New_York','01:30','7',epoch('2026-11-01T00:00:00-04:00')), first_fold)
        self.assertEqual(next_occurrence('America/New_York','01:30','7',first_fold),
                         epoch('2026-11-08T01:30:00-05:00'))
        # Local time stays at 10:00 across DST, even though UTC changes.
        self.assertEqual(next_occurrence('America/New_York','10:00','7',epoch('2026-03-01T10:00:00-05:00')),
                         epoch('2026-03-08T10:00:00-04:00'))
        track = self.track()
        for zone, clock, days in [('Invalid/Zone','10:00','1'),('UTC','24:00','1'),
                                  ('UTC','10:00','0'),('UTC','10:00',''),('UTC','1:00','1')]:
            with self.subTest(zone=zone, clock=clock, days=days), self.assertRaises(StationError):
                self.station.action('program.add', {'track_id':track['id'], 'timezone':zone,
                                                  'local_time':clock, 'weekdays':days})
        self.assertEqual(self.station.status()['programs'], [])

    def test_recurring_and_one_shot_events_merge_in_due_order(self):
        track = self.track('Recurring', sample=11, kind='ad')
        shot = self.track('One-shot', sample=22, kind='speech')
        base = dt.datetime(2026, 1, 5, 9, tzinfo=dt.timezone.utc).timestamp()
        with patch('myradio.station.time.time', return_value=base):
            self.station.action('program.add', {'track_id':track['id'], 'timezone':'UTC',
                                               'local_time':'10:00', 'weekdays':'1'})
            self.station.action('schedule.add', {'track_id':shot['id'], 'at':'2026-01-05T10:00:10+00:00'})
        self.station.action('transport.start')
        self.assertEqual(self.station.tick(now=base + 3620), struct.pack('<hh',11,11) * FRAMES)
        self.assertEqual(self.station.status()['queue'][0]['track_id'], shot['id'])
        self.assertEqual(self.station.tick(now=base + 3620), struct.pack('<hh',22,22) * FRAMES)

    def test_nearly_full_queue_admits_earliest_due_program_before_later_events(self):
        early = self.track('Early program', sample=11, kind='ad')
        later = self.track('Later one-shot', sample=22, kind='speech')
        filler = self.track('Ordinary music', sample=33)
        base = dt.datetime(2026, 1, 5, 9, tzinfo=dt.timezone.utc).timestamp()
        with patch('myradio.station.time.time', return_value=base):
            self.station.action('program.add', {'track_id':early['id'], 'timezone':'UTC',
                                               'local_time':'10:00', 'weekdays':'1'})
            self.station.action('schedule.add', {'track_id':later['id'], 'at':'2026-01-05T10:00:10+00:00'})
        self.station.db.executemany('INSERT INTO queue(track_id) VALUES (?)', [(filler['id'],)] * 999)
        self.station.db.commit()
        self.station.action('transport.start')
        self.assertEqual(self.station.tick(now=base + 3620), struct.pack('<hh',11,11) * FRAMES)
        state = self.station.status()
        self.assertEqual(state['programs'][0]['last_status'], 'queued')
        self.assertEqual(state['schedules'][0]['status'], 'missed')
        self.assertEqual(len(state['queue']), 999)
        self.assertTrue(all(q['track_id'] == filler['id'] for q in state['queue']))

    def test_schedule_capacity_preserves_order_and_does_not_admit_stale_events(self):
        filler = self.track('Ordinary music', sample=33)
        events = [self.track(str(i), sample=i + 1, kind='speech') for i in range(4)]
        now = 1000
        self.station.db.executemany('INSERT INTO queue(track_id) VALUES (?)', [(filler['id'],)] * 998)
        self.station.db.executemany('INSERT INTO schedules VALUES (?,?,?,?,?)',
                                   [(str(i), t['id'], 'test', now - 70 + i * 20, 'pending')
                                    for i, t in enumerate(events)])
        self.station.db.commit()
        self.station.action('transport.start')
        # Event 0 is stale; events 1 and 2 get the two free slots, not event 3.
        self.assertEqual(self.station.tick(now=now), struct.pack('<hh',2,2) * FRAMES)
        state = self.station.status()
        self.assertEqual(state['queue'][0]['track_id'], events[2]['id'])
        self.assertEqual({s['id']:s['status'] for s in state['schedules']},
                         {'0':'missed', '1':'queued', '2':'queued', '3':'missed'})
        self.assertEqual(len(state['queue']), 999)

    def test_full_queue_marks_due_events_missed_without_displacing_music(self):
        filler = self.track('Ordinary music', sample=33)
        event = self.track('No room', sample=44, kind='ad')
        self.station.db.executemany('INSERT INTO queue(track_id) VALUES (?)', [(filler['id'],)] * 1000)
        self.station.db.execute('INSERT INTO schedules VALUES (?,?,?,?,?)',
                                ('event', event['id'], 'test', 100, 'pending'))
        self.station.db.commit()
        self.station.action('transport.start')
        self.assertEqual(self.station.tick(now=100), struct.pack('<hh',33,33) * FRAMES)
        state = self.station.status()
        self.assertEqual(state['schedules'][0]['status'], 'missed')
        self.assertEqual(len(state['queue']), 999)

    def test_format_validation_and_mono_normalization(self):
        track = self.track()
        self.station.action('transport.start')
        self.assertEqual(self.station.tick(), struct.pack('<hh', 1234, 1234) * FRAMES)
        self.assertEqual(self.station.status()['current']['id'], track['id'])
        for data in [b'garbage', audio(rate=48000), audio(frames=1), audio()[:-8]]:
            with self.subTest(size=len(data)), self.assertRaises(StationError):
                self.station.import_wav(data, 'Invalid')
        self.assertEqual(len(self.station.status()['library']), 1)

    def test_rotation_visits_entire_library_not_only_first_two(self):
        tracks = [self.track(str(i), sample=i+1) for i in range(4)]
        self.station.action('transport.start')
        for index in range(12):
            block = self.station.tick()
            self.assertEqual(block, struct.pack('<hh', index % 4 + 1, index % 4 + 1) * FRAMES)
            self.assertEqual(self.station.status()['current']['id'], tracks[index % 4]['id'])

    def test_queue_priority_and_gapless_track_boundaries(self):
        first = self.track('First', 11, frames=FRAMES + FRAMES // 2)
        second = self.track('Second', 22)
        self.station.action('station.update', {'auto_fill':False})
        for t in (first,second):
            self.station.action('queue.add', {'track_id':t['id']})
        self.station.action('transport.start')
        self.station.tick()
        block = self.station.tick()
        self.assertEqual(block[:len(SILENCE)//2], struct.pack('<hh',11,11)*(FRAMES//2))
        self.assertEqual(block[len(SILENCE)//2:], struct.pack('<hh',22,22)*(FRAMES//2))
        self.station.tick()
        self.assertEqual(self.station.tick(), SILENCE)

    def test_stop_requeues_and_restart_is_not_live(self):
        track = self.track(frames=FRAMES*5)
        self.station.action('transport.start'); self.station.tick()
        self.station.action('transport.stop')
        state = self.station.status()
        self.assertIsNone(state['current']); self.assertFalse(state['running'])
        self.assertEqual(state['queue'][0]['track_id'],track['id'])
        self.station.action('station.update', {'name':'Persistent FM'})
        self.station.action('transport.start'); self.station.tick()
        self.station.close()
        self.station = Station(self.temp.name, start_worker=False)
        state=self.station.status()
        self.assertFalse(state['running']); self.assertEqual(state['station']['name'],'Persistent FM')
        self.assertEqual(len(state['queue']),1)
        self.assertEqual(state['queue'][0]['track_id'],track['id'])

    def test_single_writer_lock(self):
        with self.assertRaises(StationError):
            Station(self.temp.name, start_worker=False)

    def test_ads_and_speech_are_never_autofilled(self):
        self.track('Ad',kind='ad'); self.track('ID',kind='speech')
        self.station.action('transport.start')
        self.assertEqual(self.station.tick(),SILENCE)
        self.assertIsNone(self.station.status()['current'])

    def test_schedule_order_timezone_and_missed_events(self):
        tracks=[self.track(str(i)) for i in range(3)]
        now=time.time()
        for index,track in enumerate(tracks):
            at=dt.datetime.fromtimestamp(now+10+index, dt.timezone(dt.timedelta(hours=5))).isoformat()
            self.station.action('schedule.add', {'track_id':track['id'],'at':at})
        with self.assertRaises(StationError):
            self.station.action('schedule.add', {'track_id':tracks[0]['id'],'at':'2030-01-01T10:00:00'})
        self.station.action('transport.start');self.station.tick(now+20)
        state=self.station.status()
        self.assertEqual(state['current']['id'],tracks[0]['id'])
        self.assertEqual([x['track_id'] for x in state['queue']],[x['id'] for x in tracks[1:]])
        self.assertTrue(all(x['status']=='queued' for x in state['schedules']))
        self.station.action('schedule.add',{'track_id':tracks[0]['id'],'at':dt.datetime.fromtimestamp(now+30,dt.timezone.utc).isoformat()})
        self.station.action('transport.stop');self.station.tick(now+31)
        self.assertIn('missed',[x['status'] for x in self.station.status()['schedules']])

    def test_slow_listener_is_disconnected_without_stalling(self):
        listener=self.station.subscribe()
        for _ in range(31):self.station.tick()
        state=self.station.status()
        self.assertEqual(state['stream'],{'listeners':0,'dropped':1})
        self.assertEqual(listener.qsize(),30)
        self.assertIsNone(list(listener.queue)[-1])

    def test_delete_and_input_guards(self):
        track=self.track()
        self.station.action('queue.add',{'track_id':track['id']})
        with self.assertRaises(StationError):self.station.action('library.delete',{'track_id':track['id']})
        entry=self.station.status()['queue'][0]
        self.station.action('queue.remove',{'queue_id':entry['id']})
        self.station.action('library.delete',{'track_id':track['id']})
        self.assertEqual(list((Path(self.temp.name)/'media').iterdir()),[])
        for op,args in [('station.update',{'name':'OK','tagline':''}),('station.update',{'auto_fill':'yes'}),('queue.add',{}),([],{}),('transport.start',{'x':1})]:
            with self.subTest(op=op),self.assertRaises(StationError):self.station.action(op,args)
        self.assertEqual(self.station.status()['station']['name'],'My radio')

    def test_commands_and_model_proposals_need_approval(self):
        self.track()
        with patch.dict('os.environ',{},clear=True): chat=Chat(self.station)
        self.assertEqual(chat.message('help')['mode'],'commands')
        chat.message('start');self.assertTrue(self.station.running)
        chat.message('stop');self.assertFalse(self.station.running)
        answer={'choices':[{'message':{'content':'I propose renaming the station.','tool_calls':[{'function':{'name':'station_update','arguments':'{"name":"Night FM"}'}}]}}]}
        with patch.dict('os.environ',{'MYRADIO_AGENT_URL':'http://127.0.0.1:9999/v1','MYRADIO_AGENT_MODEL':'test'},clear=True):chat=Chat(self.station)
        with patch.object(ModelTransport,'post',return_value=json.dumps(answer).encode()): result=chat.message('Rename to Night FM')
        self.assertEqual(self.station.status()['station']['name'],'My radio')
        chat.approve(result['actions'][0]['id'])
        self.assertEqual(self.station.status()['station']['name'],'Night FM')
        with self.assertRaises(StationError):chat.approve(result['actions'][0]['id'])


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.station=Station(self.temp.name)
        import os
        self.env=patch.dict('os.environ',{'PATH':os.environ.get('PATH',''), 'HOME':os.environ.get('HOME','')},clear=True);self.env.start()
        self.token=load_token(self.temp.name)
        self.server=Server(('127.0.0.1',0),self.station,self.token)
        self.port=self.server.server_address[1]
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.client=Client(self.port,self.token)
        self.http=urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.station.close();self.thread.join()
        self.env.stop();self.temp.cleanup()

    def request(self,path,body=None,headers=None):
        return self.http.open(urllib.request.Request(f'http://127.0.0.1:{self.port}'+path,body,headers or {}),timeout=3)

    def test_auth_origin_session_and_real_audio_stream(self):
        token_path=self.station.directory/'control.token'
        token_path.write_text('invalid token '+('x'*32))
        with self.assertRaises(StationError):load_token(self.station.directory)
        token_path.write_text(self.token)
        with self.assertRaises(urllib.error.HTTPError) as e:self.request('/api/status')
        self.assertEqual(e.exception.code,401)
        with self.assertRaises(urllib.error.HTTPError) as e:self.request('/api/status',headers={'X-Myradio-Token':self.token,'Origin':'https://evil.example'})
        self.assertEqual(e.exception.code,403)
        with self.request('/api/session',b'{}',{'X-Myradio-Token':self.token,'Content-Type':'application/json'}) as response:
            cookie=response.headers['Set-Cookie'].split(';')[0]
        headers={'Cookie':cookie}
        with self.request('/api/session',b'{}',dict(headers,**{'Content-Type':'application/json'})) as response:
            self.assertIsNone(response.headers.get('Set-Cookie'))
        self.assertEqual(len(self.server.sessions),1)
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.request('/api/status',headers={'X-Myradio-Token':'\u00e9'})
        self.assertEqual(e.exception.code,401)
        with self.request('/api/status',headers=headers) as response:self.assertFalse(json.load(response)['running'])
        with self.request('/api/upload?title=Real%20audio',audio(2222,FRAMES*10),{'X-Myradio-Token':self.token}) as response:
            track=json.load(response)
        self.client.request('/api/action',{'operation':'transport.start'})
        time.sleep(.15)
        with self.request('/stream.wav',headers=headers) as response:
            data=response.read(44+len(SILENCE))
        self.assertEqual(data[:4],b'RIFF')
        self.assertEqual(data[44:],struct.pack('<hh',2222,2222)*FRAMES)
        self.assertEqual(self.client.request('/api/status')['current']['id'],track['id'])
        with self.request('/api/logout',b'{}',{'Cookie':cookie,'Content-Type':'application/json'}):pass
        with self.assertRaises(urllib.error.HTTPError):self.request('/api/status',headers=headers)

    def test_explicit_private_proxy_origin_keeps_auth_and_exact_origin_checks(self):
        remote = 'https://turbo.test.ts.net:8443'
        remote_headers = {'Host':'turbo.test.ts.net:8443', 'Origin':remote, 'X-Myradio-Token':self.token}
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.request('/api/status', headers=remote_headers)
        self.assertEqual(e.exception.code,403)
        self.server.public_origin = remote
        with self.request('/api/status', headers=remote_headers) as r:
            self.assertEqual(r.status,200)
        for changes, code in [({'Origin':'https://evil.example'},403), ({'Host':'evil.example'},403),
                              ({'X-Myradio-Token':''},401), ({'Sec-Fetch-Site':'cross-site'},403)]:
            with self.assertRaises(urllib.error.HTTPError) as e:
                self.request('/api/status', headers=dict(remote_headers, **changes))
            self.assertEqual(e.exception.code,code)
        with self.request('/api/status', headers={'X-Myradio-Token':self.token}) as r:
            self.assertEqual(r.status,200)

    def test_saved_agent_actual_http_exchange_and_approval(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        received=[]
        class Provider(BaseHTTPRequestHandler):
            def do_POST(handler):
                body=json.loads(handler.rfile.read(int(handler.headers['Content-Length'])))
                received.append((handler.path,handler.headers.get('Authorization'),body))
                answer={'choices':[{'message':{'content':'I propose the requested rename.', 'tool_calls':[{'function':{'name':'station_update','arguments':json.dumps({'name':'Network Agent FM'})}}]}}]}
                payload=json.dumps(answer).encode()
                handler.send_response(200);handler.send_header('Content-Type','application/json');handler.send_header('Content-Length',str(len(payload)));handler.end_headers();handler.wfile.write(payload)
            def log_message(*args):pass
        provider=ThreadingHTTPServer(('127.0.0.1',0),Provider)
        thread=threading.Thread(target=provider.serve_forever,daemon=True);thread.start()
        config={'url':f'http://127.0.0.1:{provider.server_address[1]}/v1','model':'fake-network-provider','key':'test-network-key'}
        headers={'X-Myradio-Token':self.token,'Content-Type':'application/json'}
        try:
            with self.request('/api/agent',json.dumps(config).encode(),headers):pass
            with self.request('/api/chat',json.dumps({'message':'Rename to Network Agent FM'}).encode(),headers) as response:
                answer=json.load(response)
            self.assertEqual(received[0][0],'/v1/chat/completions')
            self.assertEqual(received[0][1],'Bearer test-network-key')
            self.assertEqual(received[0][2]['model'],'fake-network-provider')
            self.assertNotIn('test-network-key',json.dumps(received[0][2]))
            self.assertNotEqual(self.station.status()['station']['name'],'Network Agent FM')
            with self.request('/api/approve',json.dumps({'id':answer['actions'][0]['id']}).encode(),headers):pass
            self.assertEqual(self.station.status()['station']['name'],'Network Agent FM')
            # Real socket failure, not a mocked urllib exception.
            provider.shutdown();provider.server_close();thread.join()
            with self.assertRaises(urllib.error.HTTPError) as down:
                self.request('/api/chat',json.dumps({'message':'Change station again'}).encode(),headers)
            self.assertEqual(down.exception.code,400)
            self.assertEqual(self.station.status()['station']['name'],'Network Agent FM')
            self.assertEqual(self.server.chat.pending,{})
        finally:
            provider.shutdown();provider.server_close();thread.join(timeout=2)

    def test_private_agent_settings_persist_disconnect_and_preserve_approval(self):
        import os
        from myradio.agent_settings import validate_agent, AgentSettingsError
        headers={'X-Myradio-Token':self.token,'Content-Type':'application/json'}
        config={'url':'http://127.0.0.1:12345/v1','model':'test-operator','key':'private-agent-key'}
        with self.assertRaises(urllib.error.HTTPError) as denied:
            self.request('/api/agent',json.dumps(config).encode(),{'Content-Type':'application/json'})
        self.assertEqual(denied.exception.code,401)
        for url in ['http://example.org/v1','https://user:pass@example.org/v1','https://example.org/v1?token=x','https://example.org:bad/v1']:
            with self.assertRaises(AgentSettingsError):validate_agent(dict(config,url=url))
        with self.request('/api/agent',json.dumps(config).encode(),headers) as response:
            self.assertNotIn(config['key'],response.read().decode())
        path=self.station.directory/'agent.json'
        self.assertEqual(os.stat(path).st_mode & 0o777,0o600)
        self.assertEqual(json.loads(path.read_text()),config)
        with self.request('/api/agent',headers=headers) as response:
            summary=json.load(response)
            self.assertEqual(summary['model'],config['model'])
            self.assertTrue(summary['key_saved'])
            self.assertNotIn(config['key'],json.dumps(summary))
        self.assertNotIn(config['url'],json.dumps(self.station.status()))
        self.assertNotIn(config['key'],json.dumps(self.station.status()))
        reloaded=Chat(self.station)
        self.assertEqual(reloaded.endpoint,config['url'])
        self.assertEqual(reloaded.key,config['key'])
        fake={'choices':[{'message':{'content':'Propose a rename.', 'tool_calls':[{'function':{'name':'station_update','arguments':json.dumps({'name':'Approved AI FM'})}}]}}]}
        with patch.object(ModelTransport,'post',return_value=json.dumps(fake).encode()) as post:
            result=self.server.chat.message('Rename the station')
            self.assertNotEqual(self.station.status()['station']['name'],'Approved AI FM')
            url,data,sent=post.call_args.args
            self.assertEqual(sent['Authorization'],'Bearer '+config['key'])
            self.assertNotIn(config['key'],data.decode())
            self.assertNotIn(config['url'],data.decode())
            self.server.chat.approve(result['actions'][0]['id'])
            self.assertEqual(self.station.status()['station']['name'],'Approved AI FM')
        # Settings cannot race an in-flight chat; local playout lock is unrelated.
        self.station.chat_lock.acquire()
        try:
            with self.assertRaises(StationError):self.server.chat.configure({'disconnect':True})
        finally:self.station.chat_lock.release()
        self.server.chat.pending['old']=(time.monotonic()+120,{})
        with self.request('/api/agent',json.dumps({'disconnect':True}).encode(),headers) as response:
            self.assertFalse(json.load(response)['configured'])
        self.assertFalse(path.exists());self.assertFalse(self.server.chat.pending);self.assertFalse(self.server.chat.history)
        self.assertEqual(self.station.chat_mode,'commands')
        with patch.dict('os.environ',{'MYRADIO_AGENT_URL':config['url'],'MYRADIO_AGENT_MODEL':'env-model'},clear=True):
            environment=Chat(self.station)
            self.assertTrue(environment.settings()['environment_managed'])
            with self.assertRaises(StationError):environment.configure(config)

    def test_upload_converts_audio_and_rejects_bad_input(self):
        import shutil
        if not shutil.which('ffmpeg'):self.skipTest('FFmpeg needed for HTTP conversion verification')
        headers={'X-Myradio-Token':self.token}
        with self.request('/api/upload?title=Resampled&kind=speech',audio(rate=48000,frames=4800),headers) as response:
            item=json.load(response)
        self.assertEqual(item['kind'],'speech')
        self.assertAlmostEqual(item['duration'],.1,places=2)
        self.assertTrue(self.server.upload_slot.acquire(blocking=False))
        self.server.upload_slot.release()
        with self.assertRaises(urllib.error.HTTPError) as invalid:
            self.request('/api/upload?title=Bad',b'not audio',headers)
        self.assertEqual(invalid.exception.code,400)
        self.assertTrue(self.server.upload_slot.acquire(blocking=False))
        self.server.upload_slot.release()
        self.assertEqual(len(self.station.status()['library']),1)

    def test_broadcast_configuration_is_authenticated_and_private(self):
        config=dict(url='http://127.0.0.1:1/live.mp3',username='source',password='http-private-secret')
        with self.assertRaises(urllib.error.HTTPError) as denied:
            self.request('/api/broadcast',json.dumps(config).encode(),{'Content-Type':'application/json'})
        self.assertEqual(denied.exception.code,401)
        headers={'X-Myradio-Token':self.server.token,'Content-Type':'application/json'}
        with self.request('/api/broadcast',json.dumps(config).encode(),headers) as response:
            result=response.read().decode()
            self.assertNotIn(config['password'],result)
        with self.request('/api/broadcast',headers=headers) as response:
            self.assertEqual(json.load(response),{'url':config['url'],'username':'source'})
        result=dispatch(self.client,'tools/call',{'name':'station_status','arguments':{}})
        self.assertNotIn(config['password'],json.dumps(result))
        self.assertNotIn(config['url'],json.dumps(result))
        self.assertTrue(dispatch(self.client,'tools/call',{'name':'broadcast_start','arguments':{}})['isError'])
        self.station.import_wav(audio(),'Test')
        self.station.action('transport.start')
        with patch.object(self.station.broadcast,'start',return_value={}) as start:
            self.assertFalse(dispatch(self.client,'tools/call',{'name':'broadcast_start','arguments':{}})['isError'])
            self.assertEqual(start.call_count,1)
        self.assertFalse(dispatch(self.client,'tools/call',{'name':'broadcast_stop','arguments':{}})['isError'])

    def test_assets_and_mcp_share_the_same_state(self):
        for path,mime in [('/','text/html'),('/app.js','text/javascript'),('/style.css','text/css')]:
            with self.request(path) as response:
                self.assertTrue(response.read());self.assertIn(mime,response.headers['Content-Type'])
                self.assertIn("frame-ancestors 'none'",response.headers['Content-Security-Policy'])
        result=dispatch(self.client,'tools/call',{'name':'station_update','arguments':{'name':'Agent FM'}})
        self.assertFalse(result['isError'])
        self.assertEqual(self.station.status()['station']['name'],'Agent FM')
        self.assertEqual({t['name'] for t in dispatch(self.client,'tools/list',{})['tools']}, {'station_status'} | {t['function']['name'] for t in tools()})
        self.assertIn('program_add', {t['name'] for t in dispatch(self.client,'tools/list',{})['tools']})
        track = self.station.import_wav(audio(), 'MCP ID', kind='speech')
        created = dispatch(self.client, 'tools/call', {'name':'program_add', 'arguments':{
            'track_id':track['id'], 'timezone':'UTC', 'local_time':'13:00', 'weekdays':'1,5'}})
        self.assertFalse(created['isError'])
        with self.request('/api/status', headers={'X-Myradio-Token':self.token}) as response:
            program = json.load(response)['programs'][0]
        self.assertEqual(program['weekdays'], '1,5')
        removed = dispatch(self.client, 'tools/call', {'name':'program_remove', 'arguments':{'program_id':program['id']}})
        self.assertFalse(removed['isError'])
        self.assertEqual(self.station.status()['programs'], [])
        source=io.StringIO('\n'.join([json.dumps({'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-03-26'}}),json.dumps({'jsonrpc':'2.0','method':'notifications/initialized'}),json.dumps({'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'station_status'}})])+'\n')
        sink=io.StringIO();serve(self.client,source,sink)
        results=[json.loads(line) for line in sink.getvalue().splitlines()]
        self.assertEqual(len(results),2)
        self.assertEqual(results[0]['result']['protocolVersion'],'2025-03-26')
        self.assertIn('Agent FM',results[1]['result']['content'][0]['text'])



class ImportTests(unittest.TestCase):
    def test_native_wav_without_encoder_and_metadata_validation(self):
        with tempfile.TemporaryDirectory() as d:
            s=Station(d,start_worker=False)
            try:
                with patch('myradio.importing.shutil.which',return_value=None):
                    item=s.import_audio(audio(), 'Native')
                    self.assertEqual(item['duration'],.1)
                    with self.assertRaisesRegex(StationError,'needs FFmpeg'):
                        s.import_audio(b'not wav','Unsupported')
                with patch('myradio.station.decode_audio') as decode:
                    for params in [('', '', 'music'), ('Bad',None,'music'), ('Bad','','video')]:
                        with self.assertRaises(StationError):s.import_audio(b'file',*params)
                    decode.assert_not_called()
                self.assertEqual(len(s.status()['library']),1)
            finally:s.close()

    def test_failed_database_import_rolls_back_before_playout_can_commit(self):
        with tempfile.TemporaryDirectory() as d:
            s=Station(d,start_worker=False)
            try:
                # Simulate a failing history insert after the track insert has run.
                s.db.execute("CREATE TRIGGER fail_import BEFORE INSERT ON history BEGIN SELECT RAISE(FAIL, 'storage failure'); END")
                with self.assertRaisesRegex(StationError, 'restart'):s.import_audio(audio(),'Failed import')
                self.assertFalse(s.db.in_transaction)
                s.tick()  # Must not commit a dangling track after cleanup.
                self.assertEqual(s.status()['library'],[])
                self.assertEqual(list(s.media.iterdir()),[])
                s.db.execute('DROP TRIGGER fail_import')
                # Fixing storage must not silently clear the fault latch. Restart
                # is required even when the triggering write failure is removed.
                with self.assertRaisesRegex(StationError, 'restart'):s.import_audio(audio(),'Retry before restart')
                self.assertEqual(list(s.media.iterdir()),[])
                s.close()
                s=Station(d,start_worker=False)
                self.assertFalse(s.status()['running'])
                self.assertEqual(s.import_audio(audio(),'Retry')['title'],'Retry')
                self.assertEqual(len(s.status()['library']),1)
            finally:s.close()

    def test_common_formats_real_decoder_and_failure_cleanup(self):
        import shutil
        import subprocess
        from myradio.importing import decode_audio, ImportError as AudioImportError
        if not shutil.which('ffmpeg'):self.skipTest('FFmpeg needed for real audio conversion')
        with tempfile.TemporaryDirectory() as d:
            s=Station(d)
            try:
                for mux,codec in [('mp3','libmp3lame'),('flac','flac'),('ogg','libvorbis'),('adts','aac')]:
                    with self.subTest(format=mux):
                        encoded=subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-f','wav','-i','pipe:0',
                                                '-ar','48000','-ac','1','-c:a',codec,'-f',mux,'pipe:1'],
                                               input=audio(frames=FRAMES*3),capture_output=True,timeout=20)
                        self.assertEqual(encoded.returncode,0,encoded.stderr)
                        item=s.import_audio(encoded.stdout,mux)
                        self.assertTrue(.25<item['duration']<.5)
                        with wave.open(str(s.media/(item['id']+'.wav')),'rb') as f:
                            self.assertEqual((f.getframerate(),f.getnchannels(),f.getsampwidth()),(44100,2,2))
                            f.setpos(4410)  # Skip lossy-codec priming delay.
                            self.assertGreater(max(struct.unpack('<'+'h'*200,f.readframes(100))),100)
                # Seekable M4A input handles indexes at either end of the file.
                target=Path(d)/'test.m4a'
                encoded=subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-f','wav','-i','pipe:0',
                                        '-c:a','aac',str(target)],input=audio(frames=FRAMES*3),capture_output=True,timeout=20)
                self.assertEqual(encoded.returncode,0,encoded.stderr)
                s.import_audio(target.read_bytes(),'M4A');target.unlink()
                target=Path(d)/'test.aiff'
                encoded=subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-f','wav','-i','pipe:0',
                                        '-c:a','pcm_s16be',str(target)],input=audio(frames=FRAMES*3),capture_output=True,timeout=20)
                self.assertEqual(encoded.returncode,0,encoded.stderr)
                s.import_audio(target.read_bytes(),'AIFF');target.unlink()
                s.import_audio(audio(rate=48000,frames=48000),'48 kHz WAV')
                s.action('transport.start')
                for bad in [b'not audio',b'#EXTM3U\nhttp://127.0.0.1/secret.mp3\n', b'RIFF'+b'0'*100]:
                    with self.assertRaises(StationError):s.import_audio(bad,'Bad')
                with self.assertRaisesRegex(AudioImportError,'normalized'):
                    decode_audio(audio(frames=FRAMES*3),d,limit=17640)
                with self.assertRaisesRegex(AudioImportError,'exceeded'):
                    decode_audio(audio(frames=FRAMES*3),d,timeout=0)
                self.assertEqual(len(s.status()['library']),7)
                self.assertEqual(list(Path(d).glob('import-*')),[])
                self.assertTrue(s.running)
                self.assertIsNotNone(s.status()['current'])
            finally:s.close()


class BroadcastTests(unittest.TestCase):
    """Broadcast failures must not block the local clock or reveal credentials."""
    def wait(self, predicate, timeout=8):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return
            time.sleep(.03)
        self.fail('Timed out waiting for broadcast state')

    def test_config_private_restart_off_and_agent_approval(self):
        import os
        from myradio.broadcast import BroadcastError, validate_config
        from myradio.chat import tools
        with tempfile.TemporaryDirectory() as d:
            s = Station(d, start_worker=False)
            try:
                config = dict(url='http://127.0.0.1:18999/live.mp3', username='source', password='private-test-password')
                for bad in ['http://example.org/live.mp3', 'https://u:p@example.org/live', 'file:///live',
                            'https://example.org/live?token=secret', 'https://example.org/live\r\nX:evil', 'https://example.org:bad/live']:
                    with self.subTest(url=bad), self.assertRaises(BroadcastError):
                        validate_config(dict(config, url=bad))
                s.broadcast.configure(config)
                self.assertEqual(os.stat(Path(d)/'broadcast.json').st_mode & 0o777, 0o600)
                self.assertEqual(json.loads((Path(d)/'broadcast.json').read_text()), config)
                self.assertNotIn(config['password'], json.dumps(s.status()))
                self.assertNotIn(config['url'], json.dumps(s.status()))
                self.assertFalse(any('password' in json.dumps(tool) for tool in tools()))
                with self.assertRaises(StationError):
                    s.action('broadcast.start')
                s.import_wav(audio(), 'Test')
                s.action('transport.start')
                chat = Chat(s)
                chat.pending['approve'] = (time.monotonic()+120, {'operation':'broadcast.start','arguments':{}})
                with patch.object(s.broadcast, 'start', return_value={}) as start:
                    self.assertFalse(start.called)
                    chat.approve('approve')
                    self.assertEqual(start.call_count, 1)
                with patch('myradio.broadcast.os.fsync') as fsync:
                    # A saved config write cannot acquire the lock used by status/clock.
                    def check(fd):
                        done=threading.Event()
                        worker=threading.Thread(target=lambda:(s.status(), s.tick(), done.set()))
                        worker.start()
                        self.assertTrue(done.wait(1), 'config disk I/O blocked station status/tick')
                        worker.join()
                    fsync.side_effect=check
                    s.broadcast.configure(config)
            finally:
                s.close()
            s = Station(d, start_worker=False)
            try:
                self.assertTrue(s.status()['broadcast']['configured'])
                self.assertEqual(s.status()['broadcast']['state'], 'disconnected')
                self.assertFalse(s.running)
            finally:
                s.close()

    def test_backpressure_is_bounded_and_clock_is_independent(self):
        from myradio.broadcast import Broadcast
        from myradio.station import SILENCE
        with tempfile.TemporaryDirectory() as d:
            b = Broadcast(d)
            b.active=True
            for _ in range(1000):
                b.offer(SILENCE)
            self.assertEqual(b.blocks.qsize(),30)
            self.assertTrue(b.overrun.is_set())
            b.close()
            s=Station(d, start_worker=False)
            try:
                s.import_wav(audio(), 'Test'); s.action('transport.start')
                s.broadcast.configure(dict(url='http://127.0.0.1:1/live.mp3',username='source',password='secret'))
                s.action('broadcast.start')
                self.wait(lambda:s.broadcast.status()['state']=='retrying')
                end=time.monotonic()+.5
                count=0
                while time.monotonic()<end:
                    self.assertEqual(s.tick(),struct.pack('<hh',1234,1234)*FRAMES)
                    count+=1
                self.assertGreater(count,10)
                s.action('transport.stop')
                self.wait(lambda:s.broadcast.status()['state']=='disconnected')
            finally:
                s.close()

    def test_drip_fed_handshake_can_be_cancelled(self):
        import socket
        from myradio.broadcast import Broadcast
        with tempfile.TemporaryDirectory() as d, socket.socket() as listener:
            listener.bind(('127.0.0.1',0));listener.listen();listener.settimeout(5)
            stop=threading.Event(); accepted=threading.Event()
            def peer():
                try:
                    conn,_=listener.accept()
                    with conn:
                        conn.recv(8192);accepted.set()
                        # Each byte arrives before socket inactivity timeout. Stop
                        # must still interrupt this incomplete HTTP response.
                        while not stop.wait(.05):
                            conn.sendall(b'X')
                except OSError:
                    pass
            thread=threading.Thread(target=peer,daemon=True);thread.start()
            b=Broadcast(d);b.encoder=b.encoder or '/unused'
            b.configure(dict(url=f'http://127.0.0.1:{listener.getsockname()[1]}/live.mp3',username='source',password='secret'))
            try:
                b.start();self.assertTrue(accepted.wait(3))
                started=time.monotonic();b.close()
                self.assertFalse(b.thread.is_alive())
                self.assertLess(time.monotonic()-started,1)
                self.assertEqual(b.status()['state'],'disconnected')
            finally:
                b.close();stop.set();thread.join(timeout=2)

    def test_dns_wait_is_cancellable_and_does_not_spawn_retry_leaks(self):
        from myradio.broadcast import Broadcast
        with tempfile.TemporaryDirectory() as d:
            done=threading.Event();entered=threading.Event()
            def resolve(*args,**kwargs):
                entered.set();done.wait(5);return []
            b=Broadcast(d);b.encoder=b.encoder or '/unused'
            b.configure(dict(url='https://radio.invalid/live.mp3',username='source',password='secret'))
            try:
                with patch('myradio.broadcast.socket.getaddrinfo', side_effect=resolve) as dns:
                    b.start();self.assertTrue(entered.wait(2));b.close()
                    self.assertFalse(b.thread.is_alive())
                    b.start();time.sleep(.2);b.close()
                    self.assertEqual(dns.call_count,1)
            finally:
                done.set();b.close()

    def test_real_icecast_listener_decode_auth_failure_and_reconnect(self):
        import math
        import os
        import shutil
        import socket
        import subprocess
        executable=os.environ.get('MYRADIO_TEST_ICECAST')
        if not executable or not shutil.which('ffmpeg'):
            self.skipTest('Set MYRADIO_TEST_ICECAST to an Icecast 2 executable and install FFmpeg for real destination verification')
        with tempfile.TemporaryDirectory() as d:
            with socket.socket() as sock:
                sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            config=Path(d)/'icecast.xml'
            config.write_text(f'''<icecast><location>Test</location><admin>test@example.org</admin>
<limits><clients>20</clients><sources>4</sources><queue-size>524288</queue-size><source-timeout>10</source-timeout><burst-on-connect>0</burst-on-connect></limits>
<authentication><source-password>test-secret</source-password><relay-password>test-relay</relay-password><admin-user>admin</admin-user><admin-password>test-admin</admin-password></authentication>
<hostname>localhost</hostname><listen-socket><port>{port}</port><bind-address>127.0.0.1</bind-address></listen-socket><fileserve>0</fileserve>
<paths><basedir>{d}</basedir><logdir>{d}</logdir><webroot>{d}</webroot><adminroot>{d}</adminroot></paths>
<logging><accesslog>access.log</accesslog><errorlog>error.log</errorlog><loglevel>2</loglevel></logging></icecast>''')
            def start_server():
                process=subprocess.Popen([executable,'-c',str(config)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                def ready():
                    try:
                        with socket.create_connection(('127.0.0.1',port), timeout=.2):return True
                    except OSError:return False
                self.wait(ready)
                return process
            icecast=start_server()
            s=Station(Path(d)/'station')
            try:
                output=io.BytesIO()
                with wave.open(output,'wb') as f:
                    f.setparams((1,2,44100,0,'NONE','not compressed'))
                    f.writeframes(b''.join(struct.pack('<h',int(8000*math.sin(2*math.pi*440*n/44100))) for n in range(44100)))
                s.import_wav(output.getvalue(),'440 Hz broadcast test')
                s.action('transport.start')
                destination=dict(url=f'http://127.0.0.1:{port}/live.mp3',username='source',password='wrong-secret')
                s.broadcast.configure(destination);s.action('broadcast.start')
                self.wait(lambda:s.broadcast.status()['state']=='failed')
                self.assertIn('authentication',s.broadcast.status()['error'])
                self.assertEqual(s.broadcast.status()['retries'],0)
                self.assertTrue(s.running)
                self.wait(lambda:not s.broadcast.thread.is_alive())
                s.broadcast.configure(dict(destination,password='test-secret'))
                s.action('broadcast.start')
                self.wait(lambda:s.broadcast.status()['state']=='streaming')
                # Decode the public listener endpoint, NOT encoder stdout.
                # Fetch on the host, then decode via stdin. This also works when
                # the machine's ffmpeg is a container wrapper with isolated networking.
                with urllib.request.urlopen(destination['url'],timeout=5) as listener:
                    self.assertEqual(listener.headers.get_content_type(),'audio/mpeg')
                    encoded=listener.read(48000)
                decoded=subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-f','mp3','-i','pipe:0',
                                        '-t','1','-f','s16le','-ar','44100','-ac','1','pipe:1'],input=encoded,capture_output=True,timeout=15)
                self.assertEqual(decoded.returncode,0,decoded.stderr)
                self.assertEqual(len(decoded.stdout),88200)
                samples=struct.unpack('<44100h',decoded.stdout)
                rms=math.sqrt(sum(v*v for v in samples)/len(samples))
                # Discard initial MP3 encoder priming; use the final half-second
                # for a stable 440 Hz check instead of counting a variable delay.
                steady=samples[-22050:]
                crossings=sum(steady[n]<=0<steady[n+1] for n in range(len(steady)-1))
                self.assertGreater(rms,3000)
                self.assertTrue(219<=crossings<=221, crossings)
                snapshot=json.dumps(s.status())
                self.assertNotIn('test-secret',snapshot)
                self.assertNotIn(destination['url'],snapshot)
                connections=s.broadcast.status()['connections']
                icecast.terminate();icecast.wait(timeout=3)
                self.wait(lambda:s.broadcast.status()['state']=='retrying')
                self.assertTrue(s.running)
                self.assertIsNotNone(s.current)
                icecast=start_server()
                self.wait(lambda:s.broadcast.status()['state']=='streaming' and s.broadcast.status()['connections']>connections,timeout=12)
                self.assertGreater(s.broadcast.status()['retries'],0)
                before=time.monotonic();s.action('broadcast.stop')
                self.wait(lambda:s.broadcast.status()['state']=='disconnected')
                self.assertLess(time.monotonic()-before,2)
                self.assertTrue(s.running)
                s.action('broadcast.start');self.wait(lambda:s.broadcast.status()['state']=='streaming')
                s.action('transport.stop')
                self.wait(lambda:s.broadcast.status()['state']=='disconnected')
                self.assertFalse(s.running)
                print(f'\nReal Icecast listener: 44100 decoded samples, RMS={rms:.1f}, last-half-second positive crossings={crossings}; rejected auth, reconnect, independent disconnect and transport stop verified.')
            finally:
                s.close()
                icecast.terminate();icecast.wait(timeout=3)


if __name__=='__main__':
    unittest.main()
