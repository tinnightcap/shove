import imp
import json
import logging
import os
import sys
from collections import namedtuple
from subprocess import PIPE, STDOUT

import pika
from honcho.process import Process
from honcho.procfile import Procfile


logging.basicConfig(format='%(levelname)s: %(message)s', level=logging.INFO)
log = logging.getLogger(__name__)


try:
    settings = imp.load_source('shove.settings', os.environ['SHOVE_SETTINGS_FILE'])
except (ImportError, KeyError):
    log.warning('Failed to import settings from environment variable, falling back to local file.')
    try:
        from . import settings
    except ImportError:
        log.warning('Error importing settings.py, did you copy settings.py-dist yet?')
        sys.exit(1)


Order = namedtuple('Order', ('project', 'command', 'log_key', 'log_queue'))


def create_channel():
    """Create a channel for communicating with RabbitMQ."""
    params = pika.ConnectionParameters(
        host=settings.RABBITMQ_HOST,
        port=settings.RABBITMQ_PORT,
        virtual_host=settings.RABBITMQ_VHOST,
        credentials=pika.credentials.PlainCredentials(
            settings.RABBITMQ_USER,
            settings.RABBITMQ_PASS
        )
    )

    connection = pika.BlockingConnection(params)
    channel = connection.channel()
    return connection, channel


def parse_order(order_body):
    data = json.loads(order_body)
    try:
        return Order(project=data['project'], command=data['command'], log_key=data['log_key'],
                     log_queue=data['log_queue'])
    except KeyError:
        log.error('Could not parse order: `{0}`'.format(order_body))
        return None


def execute(order):
    """
    Execute the command for the project contained within the order.

    Commands are listed in a procfile within the project's repository. An order contains a project
    and a command to execute; if there are any issues with finding the procfile or the requested
    command within it, we log the issue and throw away the order.

    Commands are executed in a subprocess, but this blocks until the command is finished.

    :param order:
        The order to execute, in the form of an Order namedtuple.

    :returns:
        A tuple of (return_code, output). The output includes stdout and stderr together. If the
        command failed to execute, return_code will be 1 and the output will be an error message
        explaining the failure.
    """
    # Locate the procfile that lists the commands available for the requested project.
    project_path = settings.PROJECTS.get(order.project, None)
    if not project_path:
        msg = 'No project `{0}` found.'.format(order.project)
        log.warning(msg)
        return 1, msg

    procfile_path = os.path.join(project_path, 'bin', 'commands.procfile')
    try:
        with open(procfile_path, 'r') as f:
            procfile = Procfile(f.read())
    except IOError as err:
        msg = 'Error loading procfile for project `{0}`: {1}'.format(order.project, err)
        log.error(msg)
        return 1, msg

    command = procfile.commands.get(order.command)
    if not command:
        msg = 'No command `{0}` found in {1}'.format(order.command, procfile_path)
        log.warning(msg)
        return 1, msg

    # Execute the order and log the result. Sends stderr to stdout so we get everything in one
    # blob.
    p = Process(command, cwd=project_path, stdout=PIPE, stderr=STDOUT)
    output, err = p.communicate()
    log.info('Finished running {0} - returned {1}'.format(order.command, p.returncode))
    return p.returncode, output


def consume_message(channel, method, properties, body):
    """Consume a message from the queue."""
    order = parse_order(body)
    if order:
        log.info('Executing order: {0}'.format(order))
        return_code, output = execute(order)

        # Send the output of the command to the logging queue.
        channel.queue_declare(queue=order.log_queue, durable=True)
        body = json.dumps({
            'version': '1.0',  # Version of the logging event format.
            'log_key': order.log_key,
            'return_code': return_code,
            'output': output,
        })
        channel.basic_publish(exchange='', routing_key=order.log_queue, body=body)


def main():
    """Connect to RabbitMQ and listen for orders from the captain indefinitely."""
    connection, channel = create_channel()
    channel.queue_declare(queue=settings.QUEUE_NAME, durable=True)
    channel.basic_consume(consume_message, queue=settings.QUEUE_NAME, no_ack=True)
    log.info('Awaiting orders, sir!')
    try:
        channel.start_consuming()
    except KeyboardInterrupt:
        log.info('Standing down and returning to base, sir!')
    finally:
        connection.close(reply_text='Shove standing down, sir!')


if __name__ == '__main__':
    main()
