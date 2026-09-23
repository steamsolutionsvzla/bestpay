# coding: utf-8
from odoo import models, fields, api
from odoo.exceptions import UserError
import json
import logging

_logger = logging.getLogger(__name__)

# =====================================================
# WIZARD INTERMEDIO: Ingresar datos de consulta
# =====================================================
# Este wizard se abre cuando el usuario hace clic en 
# "🔍 Consultar Movimientos" desde el partner.
# Aquí ingresa la cuenta y el rango de fechas, y luego
# ejecuta la consulta con paginación automática.
# =====================================================
class BestpayBDVConsultaWizard(models.TransientModel):
    _name = 'bestpay_bdv.consulta.wizard'
    _description = 'Wizard para ingresar datos de consulta de movimientos BDV'
    
    partner_id = fields.Many2one('res.partner', string='Comercio', readonly=True)
    cuenta = fields.Char(
        string='Número de Cuenta',
        required=True,
        help='Número de cuenta del BDV a consultar (20 dígitos). Ej: 01020501830003283374'
    )
    fecha_ini = fields.Date(
        string='Fecha Inicio',
        required=True,
        default=fields.Date.today,
        help='Fecha desde la cual consultar movimientos'
    )
    fecha_fin = fields.Date(
        string='Fecha Fin',
        required=True,
        default=fields.Date.today,
        help='Fecha hasta la cual consultar movimientos'
    )
    
    def action_consultar(self):
        """Ejecuta la consulta con paginación automática"""
        self.ensure_one()
        
        # Validar que las fechas sean lógicas
        if self.fecha_ini > self.fecha_fin:
            raise UserError("La fecha inicio no puede ser mayor que la fecha fin.")
        
        # Buscar el provider BDV
        provider = self.env['payment.provider'].sudo().search([
            ('code', '=', 'bdv'),
            ('is_bestpay_provider', '=', True),
        ], limit=1)
        
        if not provider:
            raise UserError("No hay proveedor BDV configurado en el sistema.")
        
        # Convertir fechas a formato DD/MM/YYYY (formato que exige el BDV)
        fecha_ini_str = self.fecha_ini.strftime('%d/%m/%Y')
        fecha_fin_str = self.fecha_fin.strftime('%d/%m/%Y')
        
        # 🔄 PAGINACIÓN AUTOMÁTICA
        all_movements = []
        nro_movimiento = ''
        max_pages = 10  # Límite de seguridad: máximo 1000 movimientos
        page = 0
        
        _logger.info(f"[BDV MOVIMIENTOS] 🚀 Iniciando consulta para cuenta {self.cuenta} "
                    f"desde {fecha_ini_str} hasta {fecha_fin_str}")
        
        while page < max_pages:
            page += 1
            _logger.info(f"[BDV MOVIMIENTOS] 📄 Consultando página {page}...")
            
            resultado = provider.bdv_consultar_movimientos(
                cuenta=self.cuenta,
                fecha_ini=fecha_ini_str,
                fecha_fin=fecha_fin_str,
                nro_movimiento=nro_movimiento,
                partner=self.partner_id,
            )
            
            if not resultado.get('success'):
                error_msg = resultado.get('message', 'Error desconocido')
                error_code = resultado.get('code', '9999')
                raise UserError(f"Error consultando movimientos (código {error_code}): {error_msg}")
            
            movements = resultado.get('movements', [])
            all_movements.extend(movements)
            
            _logger.info(f"[BDV MOVIMIENTOS] ✅ Página {page}: {len(movements)} movimientos obtenidos. "
                        f"Total acumulado: {len(all_movements)}")
            
            # Si no hay más páginas, salir del loop
            if not resultado.get('has_more'):
                break
            
            # Preparar paginación: usar nroMov del último registro
            nro_movimiento = resultado.get('last_nro_movimiento', '')
            if not nro_movimiento:
                _logger.warning("[BDV MOVIMIENTOS] ⚠️ No se recibió nroMovimiento para paginación. Deteniendo.")
                break
        
        if page >= max_pages:
            _logger.warning(f"[BDV MOVIMIENTOS] ⚠️ Se alcanzó el límite de {max_pages} páginas (1000 movimientos)")
        
        if not all_movements:
            raise UserError(f"No se encontraron movimientos para la cuenta {self.cuenta} "
                          f"en el rango {fecha_ini_str} - {fecha_fin_str}.")
        
        # Abrir wizard de resultados con los movimientos acumulados
        return {
            'type': 'ir.actions.act_window',
            'name': f'Movimientos BDV - {self.partner_id.name}',
            'res_model': 'bestpay_bdv.movimiento.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_partner_id': self.partner_id.id,
                'default_movements_json': json.dumps(all_movements, ensure_ascii=False),
                'default_cuenta': self.cuenta,
                'default_fecha_ini': fecha_ini_str,
                'default_fecha_fin': fecha_fin_str,
            }
        }
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
    _description = 'Wizard para mostrar resultados de consulta de movimientos BDV'
    
    partner_id = fields.Many2one('res.partner', string='Comercio', readonly=True)
    cuenta = fields.Char(string='Cuenta Consultada', readonly=True)
    fecha_ini = fields.Char(string='Fecha Inicio', readonly=True)
    fecha_fin = fields.Char(string='Fecha Fin', readonly=True)
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
            res['cuenta'] = self.env.context.get('default_cuenta', '')
            res['fecha_ini'] = self.env.context.get('default_fecha_ini', '')
            res['fecha_fin'] = self.env.context.get('default_fecha_fin', '')
            
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