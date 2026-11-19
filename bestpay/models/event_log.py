# -*- coding: utf-8 -*-
import uuid

from odoo import fields, models


class EventLog(models.Model):
    _name = 'event.log'
    _description = 'Event Log'
    _order = 'timestamp desc, id desc'

    reference = fields.Char(
        string='Referencia',
        default=lambda self: uuid.uuid4().hex[:12],
        index=True,
        readonly=True,
        copy=False,
    )
    timestamp = fields.Datetime(
        string='Fecha y Hora',
        default=fields.Datetime.now,
        index=True,
        readonly=True,
    )
    input = fields.Text(string='Entrada')
    output = fields.Text(string='Salida')
    meta = fields.Text(
        string='Contexto',
        readonly=True,
        help='Datos de contexto del evento en formato JSON (proveedor, '
             'transacción, cliente, etc.). Se sanea antes de guardarse.',
    )
    is_error = fields.Boolean(string='Es Error', default=False, index=True)
    origin = fields.Char(string='Origen')
    function_name = fields.Char(string='Función')
    controller = fields.Selection(
        [
            ('model', 'Modelo'),
            ('app', 'App'),
            ('thirdparty', 'Thirdparty'),
            ('frontend', 'Frontend'),
        ],
        string='Controlador',
        default='model',
    )
    user = fields.Char(string='Usuario')
    instance = fields.Char(string='Instancia')
    request_url = fields.Char(string='URL')
    http_method = fields.Char(string='Método HTTP')
    ip = fields.Char(string='IP')
    duration_ms = fields.Integer(string='Duración (ms)')
    company_id = fields.Many2one('res.company', string='Compañía')
    dedupe_key = fields.Char(
        string='Clave anti-duplicados',
        readonly=True,
        index=True,
        copy=False,
        help='Identifica fallos repetidos para no inundar Telegram.',
    )
    error_detail_ids = fields.One2many(
        'error.detail', 'event_log_id', string='Detalles de Error'
    )
