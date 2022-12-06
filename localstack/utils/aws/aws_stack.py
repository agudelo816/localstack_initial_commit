import os
import boto3
import airspeed
import requests
import json
import base64
import logging
from elasticsearch import Elasticsearch
from jsonpath_rw import jsonpath, parse
from localstack.constants import *
from localstack.utils.common import *
from localstack.utils.aws.aws_models import *

# file to override environment information (used mainly for testing Lambdas locally)
ENVIRONMENT_FILE = '.env.properties'

# set up logger
LOGGER = logging.getLogger(__name__)


class Environment(object):
    def __init__(self, region=None, prefix=None):
        # target is the runtime environment to use, e.g.,
        # 'local' for local mode
        self.region = region or DEFAULT_REGION
        # prefix can be 'prod', 'stg', 'uat-1', etc.
        self.prefix = prefix

    def apply_json(self, j):
        if isinstance(j, str):
            j = json.loads(j)
        self.__dict__.update(j)

    @staticmethod
    def from_string(s):
        parts = s.split(':')
        if len(parts) == 1:
            if s in PREDEFINED_ENVIRONMENTS:
                return PREDEFINED_ENVIRONMENTS[s]
            parts = [DEFAULT_REGION, s]
        if len(parts) > 2:
            raise Exception('Invalid environment string "%s"' % s)
        region = parts[0]
        prefix = parts[1]
        return Environment(region=region, prefix=prefix)

    @staticmethod
    def from_json(j):
        if not isinstance(obj, dict):
            j = j.to_dict()
        result = Environment()
        result.apply_json(j)
        return result

    def __str__(self):
        return '%s:%s' % (self.region, self.prefix)


PREDEFINED_ENVIRONMENTS = {
    ENV_DEV: Environment(region=REGION_LOCAL, prefix=ENV_DEV)
}


def create_environment_file(env, fallback_to_environ=True):
    try:
        save_file(ENVIRONMENT_FILE, env)
    except Exception, e:
        LOGGER.warning('Unable to create file "%s" in CWD "%s" (setting $ENV instead: %s): %s' %
            (ENVIRONMENT_FILE, os.getcwd(), fallback_to_environ, e))
        if fallback_to_environ:
            os.environ['ENV'] = env


def get_environment(env=None, region_name=None):
    """
    Return an Environment object based on the input arguments.

    Parameter `env` can be either of:
        * None (or empty), in which case the rules below are applied to (env = os.environ['ENV'] or ENV_DEV)
        * an Environment object (then this object is returned)
        * a string '<region>:<name>', which corresponds to Environment(region='<region>', prefix='<prefix>')
        * the predefined string 'dev' (ENV_DEV), which implies Environment(region='local', prefix='dev')
        * a string '<name>', which implies Environment(region=DEFAULT_REGION, prefix='<name>')

    Additionally, parameter `region_name` can be used to override DEFAULT_REGION.
    """
    if os.path.isfile(ENVIRONMENT_FILE):
        try:
            env = load_file(ENVIRONMENT_FILE)
            env = env.strip() if env else env
        except Exception, e:
            # We can safely swallow this exception. In some rare cases, os.environ['ENV'] may
            # be changed by a parallel thread executing a Lambda code. This can only happen when
            # running in the local dev/test environment, hence is not critical for prod usage.
            # If reading the file was unsuccessful, we fall back to ENV_DEV and continue below.
            pass

    if not env:
        if 'ENV' in os.environ:
            env = os.environ['ENV']
        else:
            env = ENV_DEV
    elif not is_string(env) and not isinstance(env, Environment):
        raise Exception('Invalid environment: %s' % env)

    if is_string(env):
        env = Environment.from_string(env)
    if region_name:
        env.region = region_name
    if not env.region:
        raise Exception('Invalid region in environment: "%s"' % env)
    return env


def connect_to_resource(service_name, env=None, region_name=None, endpoint_url=None):
    """
    Generic method to obtain an AWS service resource using boto3, based on environment, region, or custom endpoint_url.
    """
    return connect_to_service(service_name, client=False, env=env, region_name=region_name, endpoint_url=endpoint_url)


def connect_to_service(service_name, client=True, env=None, region_name=None, endpoint_url=None):
    """
    Generic method to obtain an AWS service client using boto3, based on environment, region, or custom endpoint_url.
    """
    env = get_environment(env, region_name=region_name)
    method = boto3.client if client else boto3.resource
    if not endpoint_url:
        if env.region == REGION_LOCAL:
            endpoint_url = os.environ['TEST_%s_URL' % (service_name.upper())]
    return method(service_name, region_name=env.region, endpoint_url=endpoint_url)


class VelocityInput:
    """Simple class to mimick the behavior of variable '$input' in AWS API Gateway integration velocity templates.
    See: http://docs.aws.amazon.com/apigateway/latest/developerguide/api-gateway-mapping-template-reference.html"""
    def __init__(self, value):
        self.value = value

    def path(self, path):
        value = self.value if isinstance(self.value, dict) else json.loads(self.value)
        jsonpath_expr = parse(path)
        result = [match.value for match in jsonpath_expr.find(value)]
        result = result[0] if len(result) == 1 else result
        return result

    def json(self, path):
        return json.dumps(self.path(path))


class VelocityUtil:
    """Simple class to mimick the behavior of variable '$util' in AWS API Gateway integration velocity templates.
    See: http://docs.aws.amazon.com/apigateway/latest/developerguide/api-gateway-mapping-template-reference.html"""
    def base64Encode(self, s):
        if not isinstance(s, str):
            s = json.dumps(s)
        return base64.b64encode(s)

    def base64Decode(self, s):
        if not isinstance(s, str):
            s = json.dumps(s)
        return base64.b64decode(s)


def render_velocity_template(template, context, as_json=False):
    t = airspeed.Template(template)
    variables = {
        'input': VelocityInput(context),
        'util': VelocityUtil()
    }
    replaced = t.merge(variables)
    if as_json:
        replaced = json.loads(replaced)
    return replaced


def dynamodb_table_arn(table_name):
    return "arn:aws:dynamodb:%s:%s:table/%s" % (DEFAULT_REGION, TEST_AWS_ACCOUNT_ID, table_name)


def dynamodb_stream_arn(table_name):
    return ("arn:aws:dynamodb:%s:%s:table/%s/stream/%s" %
        (DEFAULT_REGION, TEST_AWS_ACCOUNT_ID, table_name, timestamp()))


def lambda_function_arn(function_name, account_id=TEST_AWS_ACCOUNT_ID, env=None):
    env = get_environment(env)
    return "arn:aws:lambda:%s:%s:function:%s" % (DEFAULT_REGION, account_id, function_name)


def kinesis_stream_arn(stream_name, account_id=TEST_AWS_ACCOUNT_ID, env=None):
    env = get_environment(env)
    return "arn:aws:kinesis:%s:%s:stream/%s" % (DEFAULT_REGION, account_id, stream_name)


def dynamodb_get_item_raw(dynamodb_url, request):
    headers = mock_aws_request_headers()
    headers['X-Amz-Target'] = 'DynamoDB_20120810.GetItem'
    new_item = requests.post(dynamodb_url, data=json.dumps(request), headers=headers)
    new_item = json.loads(new_item.text)
    return new_item


def mock_aws_request_headers(service='dynamodb'):
    ctype = APPLICATION_AMZ_JSON_1_0
    if service == 'kinesis':
        ctype = APPLICATION_AMZ_JSON_1_1
    headers = {
        'Content-Type': ctype,
        'Accept-Encoding': 'identity',
        'X-Amz-Date': '20160623T103251Z',
        'Authorization': ('AWS4-HMAC-SHA256 ' +
            'Credential=ABC/20160623/us-east-1/%s/aws4_request, ' +
            'SignedHeaders=content-type;host;x-amz-date;x-amz-target, Signature=1234') % service
    }
    return headers


def get_apigateway_integration(api_id, method, path, env=None):
    apigateway = connect_to_service(service_name='apigateway', client=True, env=env)

    resources = apigateway.get_resources(
        restApiId=api_id,
        limit=100
    )
    resource_id = None
    for r in resources['items']:
        if r['path'] == path:
            resource_id = r['id']
    if not resource_id:
        raise Exception('Unable to find apigateway integration for path "%s"' % path)

    integration = apigateway.get_integration(
        restApiId=api_id,
        resourceId=resource_id,
        httpMethod=method
    )
    return integration


def connect_elasticsearch():
    es = Elasticsearch([{
        'host': LOCALHOST,
        'port': DEFAULT_PORT_ELASTICSEARCH}])
    return es


def delete_all_elasticsearch_indices(endpoint=None, env=None):
    """
    This function drops ALL indexes in Elasticsearch. Handle with care!
    """
    env = aws_util.get_environment(env)
    if env.region != REGION_LOCAL:
        raise Exception('Refusing to delete ALL Elasticsearch indices outside of local dev environment.')
    indices = aws_util.elasticsearch_get_indices(endpoint=endpoint, env=env)
    for index in indices:
        aws_util.elasticsearch_delete_index(index, endpoint=endpoint, env=env)


def delete_all_elasticsearch_data():
    """
    This function drops ALL data in the local Elasticsearch data folder. Handle with care!
    """
    data_dir = os.path.join(LOCALSTACK_ROOT_FOLDER, 'infra', 'elasticsearch', 'data', 'elasticsearch', 'nodes')
    run('rm -rf "%s"' % data_dir)


def create_kinesis_stream(stream_name, shards=1, env=None):
    env = get_environment(env)
    # stream
    stream = KinesisStream(id=stream_name, num_shards=shards)
    # producer
    conn = connect_to_service('kinesis', env=env)
    stream.connect(conn)
    stream.create()
    stream.wait_for()
    return stream
