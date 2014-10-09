# coding=utf-8

import copy
import logging
import traceback
import time
import socket
from collections import namedtuple
from functools import partial
from logging.handlers import SysLogHandler, WatchedFileHandler

import tornado.options
from tornado.options import options
from tornado.escape import to_unicode

try:
    import frontik.options
    from graypy.handler import GELFHandler, LAN_CHUNK

    class BulkGELFHandler(GELFHandler):
        @staticmethod
        def format_time(record):
            t = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.created))
            return "%s,%03d" % (t, record.msecs)

        def handle_bulk(self, records_list, stages=None, status_code=None, uri=None, method=None, **kwargs):
            if len(records_list) > 0:
                first_record = records_list[0]
            else:
                return

            record_for_gelf = copy.deepcopy(first_record)
            record_for_gelf.message = u''
            record_for_gelf.exc_info = None
            record_for_gelf.short = u"{0} {1} {2}".format(method, to_unicode(uri), status_code)
            record_for_gelf.levelno = logging.INFO
            record_for_gelf.name = None
            record_for_gelf.code = status_code

            for record in records_list:
                if record_for_gelf.name is None and hasattr(record, 'handler'):
                    record_for_gelf.name = repr(record.handler)

                message = to_unicode(record.getMessage())
                if record.levelno > record_for_gelf.levelno:
                    record_for_gelf.levelno = record.levelno
                    record_for_gelf.lineno = record.lineno
                    record_for_gelf.short = message

                # only the last exception will be sent in exc_info
                if record.exc_info is not None:
                    exception_text = u'\n' + u''.join(map(to_unicode, traceback.format_exception(*record.exc_info)))
                    record_for_gelf.exc_info = record.exc_info
                    record_for_gelf.short += exception_text

                record_for_gelf.message += u' {time} {level} {msg} \n'.format(
                    time=self.format_time(record), level=record.levelname, msg=message
                )

            if stages is not None:
                for s in stages:
                    setattr(record_for_gelf, s.name + '_stage', str(int(s.delta)))

            GELFHandler.handle(self, record_for_gelf)
            GELFHandler.close(self)

except ImportError:
    import frontik.options
    options.graylog = False

log = logging.getLogger('frontik.handler')


class ContextFilter(logging.Filter):
    def filter(self, record):
        record.name = '.'.join(filter(None, [record.name, getattr(record, 'request_id', None)]))
        return True

log.addFilter(ContextFilter())


class PerRequestLogBufferHandler(logging.Logger):
    """
    Handler for storing all LogRecords for current request in a buffer until finish
    """
    def __init__(self, name, level=logging.NOTSET):
        super(PerRequestLogBufferHandler, self).__init__(name, level)
        self.records_list = []
        self.bulk_handlers = []

    def handle(self, record):
        log.handle(record)
        self.records_list.append(record)

    def add_bulk_handler(self, handler, auto_flush=True):
        self.bulk_handlers.append((handler, auto_flush))
        if not auto_flush:
            handler.flush = partial(self.flush_bulk_handler, handler)

    def flush_bulk_handler(self, handler, **kwargs):
        handler.handle_bulk(self.records_list, **kwargs)

    def flush(self, **kwargs):
        for handler, auto_flush in self.bulk_handlers:
            if auto_flush:
                self.flush_bulk_handler(handler, **kwargs)


class RequestLogger(logging.LoggerAdapter):

    Stage = namedtuple('Stage', ('name', 'delta', 'start_delta'))

    def __init__(self, request, request_id):
        self._handler = None
        self._last_stage_time = self._start_time = request._start_time

        super(RequestLogger, self).__init__(PerRequestLogBufferHandler('frontik.handler'), {'request_id': request_id})

        self.stages = []

        # backcompatibility with logger
        self.warn = self.warning
        self.addHandler = self.logger.addHandler

        if options.graylog:
            self.logger.add_bulk_handler(
                BulkGELFHandler(options.graylog_host, options.graylog_port, LAN_CHUNK, False)
            )

    def register_handler(self, handler):
        self._handler = handler
        self.extra['handler'] = handler

    def stage_tag(self, stage_name):
        stage_end_time = time.time()
        stage_start_time = self._last_stage_time
        self._last_stage_time = stage_end_time

        delta = (stage_end_time - stage_start_time) * 1000
        start_delta = (stage_start_time - self._start_time) * 1000
        stage = RequestLogger.Stage(stage_name, delta, start_delta)

        self.stages.append(stage)
        self.debug('stage "%s" completed in %.2fms', stage.name, stage.delta, extra={'_stage': stage})

    def get_current_total(self):
        return sum(s.delta for s in self.stages)

    def log_stages(self, status_code):
        """Writes available stages, total value and status code"""

        stages_str = ' '.join('{s.name}={s.delta:.2f}'.format(s=s) for s in self.stages)
        total = sum(s.delta for s in self.stages)

        self.info(
            'timings for %(page)s : %(stages)s',
            {
                'page': repr(self._handler),
                'stages': '{0} total={1:.2f} code={2}'.format(stages_str, total, status_code)
            },
        )

    def process(self, msg, kwargs):
        if 'extra' in kwargs:
            kwargs['extra'].update(self.extra)
        else:
            kwargs['extra'] = self.extra
        return msg, kwargs

    def add_bulk_handler(self, handler, auto_flush=True):
        self.logger.add_bulk_handler(handler, auto_flush)

    def request_finish_hook(self, status_code, request_method, request_uri):
        self.logger.flush(status_code=status_code, stages=self.stages, method=request_method, uri=request_uri)


def bootstrap_logging():
    """This is a replacement for standard Tornado logging configuration."""

    root_logger = logging.getLogger()
    level = getattr(logging, options.loglevel.upper())
    root_logger.setLevel(logging.NOTSET)

    if options.logfile:
        handler = logging.handlers.WatchedFileHandler(options.logfile)
        handler.setFormatter(logging.Formatter(options.logformat))
        handler.setLevel(level)
        root_logger.addHandler(handler)

    if options.stderr_log:
        if hasattr(tornado.options, 'enable_pretty_logging'):
            # Old Tornado version
            tornado.options.enable_pretty_logging(level)

        else:
            from tornado.log import LogFormatter

            handler = logging.StreamHandler()
            handler.setFormatter(
                LogFormatter(
                    fmt=tornado.options.options.stderr_format, datefmt=tornado.options.options.stderr_dateformat
                )
            )

            handler.setLevel(level)
            root_logger.addHandler(handler)

    if options.syslog:
        try:
            syslog_handler = SysLogHandler(
                facility=SysLogHandler.facility_names[options.syslog_facility],
                address=options.syslog_address
            )
            syslog_handler.setFormatter(logging.Formatter(options.logformat))
            syslog_handler.setLevel(level)
            root_logger.addHandler(syslog_handler)
        except socket.error:
            logging.getLogger('frontik.logging').exception('cannot initialize syslog')

    for logger_name in options.suppressed_loggers:
        logging.getLogger(logger_name).setLevel(logging.WARN)
