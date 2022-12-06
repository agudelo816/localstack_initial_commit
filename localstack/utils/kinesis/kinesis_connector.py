#!/usr/bin/env python

import base64
import json
import os
import sys
import socket
import time
import traceback
from urlparse import urlparse
from amazon_kclpy import kcl
from docopt import docopt
from sh import tail
from localstack.utils.common import *
from localstack.utils.kinesis import kclipy_helper
from localstack.constants import *
from localstack.utils.common import ShellCommandThread, FuncThread
from localstack.utils.aws import aws_stack
from localstack.utils.aws.aws_models import KinesisStream


class KinesisProcessor(kcl.RecordProcessorBase):

    def __init__(self, log_file=None, processor_func=None, auto_checkpoint=True):
        self.log_file = log_file
        self.processor_func = processor_func
        self.shard_id = None
        self.checkpointer = None
        self.auto_checkpoint = auto_checkpoint

    def initialize(self, shard_id):
        if self.log_file:
            self.log("initialize '%s'" % (shard_id))
        self.shard_id = shard_id

    def process_records(self, records, checkpointer):
        if self.processor_func:
            self.processor_func(records=records,
                checkpointer=checkpointer, shard_id=self.shard_id)

    def shutdown(self, checkpointer, reason):
        if self.log_file:
            self.log("Shutdown processor for shard '%s'" % self.shard_id)
        self.checkpointer = checkpointer
        if self.auto_checkpoint:
            checkpointer.checkpoint()

    def log(self, s):
        s = '%s\n' % s
        if self.log_file:
            save_file(self.log_file, s, append=True)

    @staticmethod
    def run_processor(log_file=None, processor_func=None):
        proc = kcl.KCLProcess(KinesisProcessor(log_file, processor_func))
        proc.run()


class KinesisProcessorThread(ShellCommandThread):
    def __init__(self, params):
        multi_lang_daemon_class = 'com.atlassian.KinesisStarter'
        props_file = params['properties_file']
        cmd = kclipy_helper.get_kcl_app_command('java',
            multi_lang_daemon_class, props_file)
        ShellCommandThread.__init__(self, cmd)

    @staticmethod
    def start_consumer(kinesis_stream):
        thread = KinesisProcessorThread(kinesis_stream.stream_info)
        thread.start()
        return thread


class OutputReaderThread(FuncThread):
    def __init__(self, params):
        FuncThread.__init__(self, self.start_reading, params)
        self.running = True

    def start_reading(self, params):
        for line in tail("-n", 0, "-f", params['file'], _iter=True):
            line = line.replace('\n', '')
            if not self.running:
                return
            print ('LOG: %s' % line)

    def stop(self, quiet=True):
        self.running = False


class EventFileReaderThread(FuncThread):
    def __init__(self, events_file, callback):
        FuncThread.__init__(self, self.retrieve_loop, None)
        self.running = True
        self.events_file = events_file
        self.callback = callback

    def retrieve_loop(self, params):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(self.events_file)
        sock.listen(1)
        while self.running:
            conn, client_addr = sock.accept()
            thread = FuncThread(self.handle_connection, conn)
            thread.start()
        sock.close()

    def handle_connection(self, conn):
        socket_file = conn.makefile()
        while self.running:
            line = socket_file.readline()[:-1]
            if line == '':
                # end of socket input stream
                break
            else:
                try:
                    records = json.loads(line)
                    self.callback(records)
                except Exception, e:
                    print("Unable to process JSON line: '%s': %s. Callback: %s" %
                        (truncate(line), traceback.format_exc(e), self.callback))
        conn.close()

    def stop(self, quiet=True):
        self.running = False


# construct a stream info hash
def get_stream_info(stream_name, log_file=None, shards=None, env=None, endpoint_url=None):
    # construct stream info
    env = aws_stack.get_environment(env)
    props_file = os.path.join('/tmp/', 'kclipy.%s.properties' % short_uid())
    app_name = '%s-app' % stream_name
    stream_info = {
        'name': stream_name,
        'region': DEFAULT_REGION,
        'shards': shards,
        'properties_file': props_file,
        'log_file': log_file,
        'app_name': app_name
    }
    # set local connection
    if env.region == REGION_LOCAL:
        from localstack.constants import LOCALHOST, DEFAULT_PORT_KINESIS
        stream_info['conn_kwargs'] = {
            'host': LOCALHOST,
            'port': DEFAULT_PORT_KINESIS,
            'is_secure': False
        }
    if endpoint_url:
        if 'conn_kwargs' not in stream_info:
            stream_info['conn_kwargs'] = {}
        url = urlparse(endpoint_url)
        stream_info['conn_kwargs']['host'] = url.hostname
        stream_info['conn_kwargs']['port'] = url.port
        stream_info['conn_kwargs']['is_secure'] = url.scheme == 'https'
    return stream_info


def start_kcl_client_process(stream_name, listener_script,
        log_file=None, env=None, configs={}, endpoint_url=None):
    env = aws_stack.get_environment(env)
    # construct stream info
    stream_info = get_stream_info(stream_name, log_file, env=env, endpoint_url=endpoint_url)
    props_file = stream_info['properties_file']
    # set kcl config options
    kwargs = {
        'metricsLevel': 'NONE',
        'initialPositionInStream': 'LATEST'
    }
    # set parameters for local connection
    if env.region == REGION_LOCAL:
        from localstack.constants import LOCALHOST, DEFAULT_PORT_KINESIS, DEFAULT_PORT_DYNAMODB
        kwargs['kinesisEndpoint'] = '%s:%s' % (LOCALHOST, DEFAULT_PORT_KINESIS)
        kwargs['dynamodbEndpoint'] = '%s:%s' % (LOCALHOST, DEFAULT_PORT_DYNAMODB)
        kwargs['kinesisProtocol'] = 'http'
        kwargs['dynamodbProtocol'] = 'http'
        kwargs['disableCertChecking'] = 'true'
    kwargs.update(configs)
    # create config file
    kclipy_helper.create_config_file(config_file=props_file, executableName=listener_script,
        streamName=stream_name, applicationName=stream_info['app_name'], **kwargs)
    TMP_FILES.append(props_file)
    # start stream consumer
    stream = KinesisStream(id=stream_name, params=stream_info)
    thread_consumer = KinesisProcessorThread.start_consumer(stream)
    TMP_THREADS.append(thread_consumer)
    return thread_consumer


def generate_processor_script(events_file, log_file=None):
    script_file = os.path.join('/tmp/', 'kclipy.%s.processor.py' % short_uid())
    if log_file:
        log_file = "'%s'" % log_file
    else:
        log_file = 'None'
    content = """#!/usr/bin/env python
import os, sys, json, socket
sys.path.insert(0, '%s/lib/python2.7/site-packages')
sys.path.insert(0, '%s')
from localstack.utils.kinesis import kinesis_connector
events_file = '%s'
log_file = %s
if __name__ == '__main__':
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(events_file)
    def receive_msg(records, checkpointer, shard_id):
        try:
            sock.send(b'%%s\\n' %% json.dumps(records))
        except Exception, e:
            print("WARN: Unable to forward event: %%s" %% e)
    kinesis_connector.KinesisProcessor.run_processor(log_file=log_file, processor_func=receive_msg)
    """ % (LOCALSTACK_VENV_FOLDER, LOCALSTACK_ROOT_FOLDER, events_file, log_file)
    save_file(script_file, content)
    run('chmod +x %s' % script_file)
    TMP_FILES.append(script_file)
    return script_file


def listen_to_kinesis(stream_name, listener_func=None, processor_script=None,
        events_file=None, endpoint_url=None, log_file=None, configs={}, env=None):
    """
    High-level function that allows to subscribe to a Kinesis stream
    and receive events in a listener function. A KCL client process is
    automatically started in the background.
    """
    env = aws_stack.get_environment(env)
    if not events_file:
        events_file = os.path.join('/tmp/', 'kclipy.%s.fifo' % short_uid())
        TMP_FILES.append(events_file)
    if not processor_script:
        processor_script = generate_processor_script(events_file, log_file=log_file)

    run('rm -f %s' % events_file)
    # start event reader thread (this process)
    thread = EventFileReaderThread(events_file, listener_func)
    thread.start()
    # start KCL client (background process)
    if processor_script[-4:] == '.pyc':
        processor_script = processor_script[0:-1]
    return start_kcl_client_process(stream_name, processor_script,
        endpoint_url=endpoint_url, log_file=log_file, configs=configs, env=env)
