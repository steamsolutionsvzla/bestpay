# -*- coding: utf-8 -*-
from odoo import models, fields, api
import json

# =================================================================
# ⚠️ NOTA: MODELO TEMPORAL
# =================================================================
# Este modelo es TRANSIENT (se borra automáticamente de la BD).
# Se usa temporalmente para QA mientras definimos:
# 1. Si es legal/permitido almacenar movimientos bancarios
# 2. La estructura definitiva del modelo persistente
# 3. Los requisitos de conciliación automática
# =================================================================

class BestpayBDVMovimientoWizard(models.TransientModel):
    _name = 'bestpay_bdv.movimiento.wizard'
    _description = 'Wizard temporal para consultar movimientos BDV (QA)'
    
    partner_id = fields.Many2one('res.partner', string='Comercio', readonly=True)
    movements_json = fields.Text(string='Movimientos JSON', readonly=True)
    total_movements = fields.Integer(string='Total Movimientos', readonly=True)
    movement_lines = fields.One2many(
        'bestpay_bdv.movimiento.line',
        'wizard_id',
        string='Líneas de Movimiento'
    )
    
    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        if self.env.context.get('default_movements_json'):
            movements = json.loads(self.env.context['default_movements_json'])
            res['movements_json'] = self.env.context['default_movements_json']
            res['total_movements'] = len(movements)
            
            lines = []
            for mov in movements:
                lines.append((0, 0, {
                    'referencia': mov.get('referencia', ''),
                    'descripcion': mov.get('descripcion', ''),
                    'fecha': mov.get('fecha', ''),
                    'hora': mov.get('hora', ''),
                    'tipo': mov.get('mov', ''),
                    'importe': mov.get('importe', ''),
                    'saldo': mov.get('saldo', ''),
                    'nro_mov': mov.get('nroMov', ''),
                    'observacion': mov.get('observacion', ''),
                }))
            res['movement_lines'] = lines
        return res


class BestpayBDVMovimientoLine(models.TransientModel):
    _name = 'bestpay_bdv.movimiento.line'
    _description = 'Línea de movimiento BDV (Temporal)'
    
    wizard_id = fields.Many2one('bestpay_bdv.movimiento.wizard', string='Wizard')
    referencia = fields.Char(string='Referencia')
    descripcion = fields.Char(string='Descripción')
    fecha = fields.Char(string='Fecha')
    hora = fields.Char(string='Hora')
    tipo = fields.Char(string='Tipo (Crédito/Débito)')
    importe = fields.Char(string='Importe')
    saldo = fields.Char(string='Saldo')
    nro_mov = fields.Char(string='Nro Movimiento')
    observacion = fields.Text(string='Observación')