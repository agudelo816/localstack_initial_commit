#!/usr/bin/env python

import os
import json
import uuid
import time
import traceback
import logging
import base64
import threading
from flask import Flask, jsonify, request
from datetime import datetime
from localstack.constants import *
from localstack.utils.common import *
from localstack.utils.aws import aws_stack


APP_NAME = 'lambda_mock'
PATH_ROOT = '/2015-03-31'
ARCHIVE_FILE_PATTERN = '/tmp/lambda.handler.*.jar'
EVENT_FILE_PATTERN = '/tmp/lambda.event.*.json'
LAMBDA_EXECUTOR_JAR = os.path.join(LOCALSTACK_ROOT_FOLDER, 'localstack',
    'mock', 'target', 'lambda-executor-1.0-SNAPSHOT.jar')
LAMBDA_EXECUTOR_CLASS = 'com.atlassian.LambdaExecutor'

app = Flask(APP_NAME)

# map ARN strings to lambda function objects
lambda_arn_to_function = {}
lambda_arn_to_cwd = {}
lambda_arn_to_handler = {}

# list of event source mappings for the API
event_source_mappings = []

# logger
LOG = logging.getLogger(__name__)

# mutex for access to CWD
cwd_mutex = threading.Semaphore(1)


def cleanup():
    global lambda_arn_to_function, event_source_mappings, lambda_arn_to_cwd, lambda_arn_to_handler
    # reset the state
    lambda_arn_to_function = {}
    lambda_arn_to_cwd = {}
    lambda_arn_to_handler = {}
    event_source_mappings = []


def func_arn(function_name):
    return aws_stack.lambda_function_arn(function_name)


def add_function_mapping(lambda_name, lambda_handler, lambda_cwd=None):
    arn = func_arn(lambda_name)
    lambda_arn_to_function[arn] = lambda_handler
    lambda_arn_to_cwd[arn] = lambda_cwd


def add_event_source(function_name, source_arn):
    mapping = {
        "UUID": str(uuid.uuid4()),
        "StateTransitionReason": "User action",
        "LastModified": float(time.mktime(datetime.utcnow().timetuple())),
        "BatchSize": 100,
        "State": "Enabled",
        "FunctionArn": func_arn(function_name),
        "EventSourceArn": source_arn,
        "LastProcessingResult": "OK"
    }
    event_source_mappings.append(mapping)
    return mapping


def process_kinesis_records(records, stream_name):
    # feed records into listening lambdas
    try:
        sources = get_event_sources(source_arn=aws_stack.kinesis_stream_arn(stream_name))
        for source in sources:
            arn = source['FunctionArn']
            lambda_function = lambda_arn_to_function[arn]
            lambda_cwd = lambda_arn_to_cwd[arn]
            event = {
                'Records': []
            }
            for rec in records:
                event['Records'].append({
                    'kinesis': rec
                })
            run_lambda(lambda_function, event=event, context={}, lambda_cwd=lambda_cwd)
    except Exception, e:
        print(traceback.format_exc(e))


def get_event_sources(func_name=None, source_arn=None):
    result = []
    for m in event_source_mappings:
        if not func_name or m['FunctionArn'] in [func_name, func_arn(func_name)]:
            if not source_arn or m['EventSourceArn'].startswith(source_arn):
                result.append(m)
    return result


def run_lambda(func, event, context, suppress_output=False, lambda_cwd=None):
    if suppress_output:
        stdout_ = sys.stdout
        stderr_ = sys.stderr
        stream = cStringIO.StringIO()
        sys.stdout = stream
        sys.stderr = stream
    if lambda_cwd:
        cwd_mutex.acquire()
        previous_cwd = os.getcwd()
        os.chdir(lambda_cwd)
    try:
        if func.func_code.co_argcount == 2:
            func(event, context)
        else:
            raise Exception('Expected handler function with 2 parameters, found %s' % func.func_code.co_argcount)
    except Exception, e:
        if suppress_output:
            sys.stdout = stdout_
            sys.stderr = stderr_
        print("ERROR executing Lambda function: %s" % traceback.format_exc(e))
    finally:
        if suppress_output:
            sys.stdout = stdout_
            sys.stderr = stderr_
        if lambda_cwd:
            os.chdir(previous_cwd)
            cwd_mutex.release()


def exec_lambda_code(script, handler_function='handler', lambda_cwd=None):
    if lambda_cwd:
        cwd_mutex.acquire()
        previous_cwd = os.getcwd()
        os.chdir(lambda_cwd)
    # WARNING: we can only do exec(..) for controlled test environments - it's dangerous!
    local_vars = {}
    # make sure os.environ[...] is available in the lambda context
    local_vars['os'] = os
    try:
        exec(script, local_vars)
    except Exception, e:
        print('ERROR: Unable to exec: %s %s' % (script, traceback.format_exc(e)))
        raise e
    finally:
        if lambda_cwd:
            os.chdir(previous_cwd)
            cwd_mutex.release()
    return local_vars[handler_function]


def set_function_code(code, lambda_name):
    lambda_handler = None
    lambda_cwd = None
    if 'ZipFile' in code:
        zip_file_content = code['ZipFile']
        zip_file_content = base64.b64decode(zip_file_content)

        if is_jar_archive(zip_file_content):
            archive = ARCHIVE_FILE_PATTERN.replace('*', short_uid())
            save_file(archive, zip_file_content)
            TMP_FILES.append(archive)

            def execute(event, context):
                event_file = EVENT_FILE_PATTERN.replace('*', short_uid())
                save_file(event_file, json.dumps(event))
                TMP_FILES.append(event_file)
                class_name = lambda_arn_to_handler[func_arn(lambda_name)].split('::')[0]
                classpath = '%s:%s' % (LAMBDA_EXECUTOR_JAR, archive)
                cmd = 'java -cp %s %s %s %s' % (classpath, LAMBDA_EXECUTOR_CLASS, class_name, event_file)
                # print(cmd)
                output = run(cmd)
                LOG.info('Lambda output: %s' % output.replace('\n', '\n> '))

            lambda_handler = execute
        else:
            if 'def handler' not in zip_file_content:
                zip_file_name = 'original_file.zip'
                tmp_dir = '/tmp/zipfile.%s' % short_uid()
                run('mkdir -p %s' % tmp_dir)
                tmp_file = '%s/%s' % (tmp_dir, zip_file_name)
                save_file(tmp_file, zip_file_content)
                TMP_FILES.append(tmp_dir)
                run('cd %s && unzip %s' % (tmp_dir, zip_file_name))
                main_script = '%s/%s' % (tmp_dir, LAMBDA_MAIN_SCRIPT_NAME)
                lambda_cwd = tmp_dir
                with open(main_script, "rb") as file_obj:
                    zip_file_content = file_obj.read()

            if 'def handler' in zip_file_content:
                lambda_handler = exec_lambda_code(zip_file_content, lambda_cwd=lambda_cwd)
            else:
                raise Exception('Unable to get handler function from lambda code')
    add_function_mapping(lambda_name, lambda_handler, lambda_cwd)


@app.route('%s/functions' % PATH_ROOT, methods=['POST'])
def create_function():
    """ Create new function
        ---
        operationId: 'createFunction'
        parameters:
            - name: 'request'
              in: body
    """
    data = json.loads(request.data)
    lambda_name = data['FunctionName']
    lambda_arn_to_handler[func_arn(lambda_name)] = data['Handler']
    code = data['Code']
    set_function_code(code, lambda_name)
    result = {}
    return jsonify(result)


@app.route('%s/functions/<function>/code' % PATH_ROOT, methods=['PUT'])
def update_function_code(function):
    """ Update the code of an existing function
        ---
        operationId: 'updateFunctionCode'
        parameters:
            - name: 'request'
              in: body
    """
    data = json.loads(request.data)
    set_function_code(data, function)
    result = {}
    return jsonify(result)


@app.route('%s/functions/<function>/configuration' % PATH_ROOT, methods=['PUT'])
def update_function_configuration(function):
    """ Update the configuration of an existing function
        ---
        operationId: 'updateFunctionConfiguration'
        parameters:
            - name: 'request'
              in: body
    """
    data = json.loads(request.data)
    lambda_arn_to_handler[func_arn(function)] = data['Handler']
    result = {}
    return jsonify(result)


@app.route('%s/event-source-mappings/' % PATH_ROOT, methods=['GET'])
def list_event_source_mappings():
    """ List event source mappings
        ---
        operationId: 'listEventSourceMappings'
    """
    response = {
        'EventSourceMappings': event_source_mappings
    }
    return jsonify(response)


@app.route('%s/event-source-mappings/' % PATH_ROOT, methods=['POST'])
def create_event_source_mapping():
    """ Create new event source mapping
        ---
        operationId: 'createEventSourceMapping'
        parameters:
            - name: 'request'
              in: body
    """
    data = json.loads(request.data)
    mapping = add_event_source(data['FunctionName'], data['EventSourceArn'])
    return jsonify(mapping)


def serve(port, quiet=True):
    if quiet:
        log = logging.getLogger('werkzeug')
        log.setLevel(logging.ERROR)
    app.run(port=int(port), threaded=True, host='0.0.0.0')

if __name__ == '__main__':
    port = DEFAULT_PORT_LAMBDA
    print("Starting server on port %s" % port)
    serve(port)
