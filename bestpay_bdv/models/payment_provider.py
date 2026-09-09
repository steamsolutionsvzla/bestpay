# -*- coding: utf-8 -*-
import logging
import requests  # <-- Esto va SEPARADO, es una librería de Python
from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class PaymentProviderBDV(models.Model):
    _inherit = 'payment.provider'

    # =====================================================
    # IDENTIFICADOR ÚNICO DEL PROVEEDOR
    # =====================================================
    # Este código es CRÍTICO: Odoo lo usa para saber qué módulo
    # maneja este provider. Debe coincidir con el nombre del módulo.
    code = fields.Selection(
        selection_add=[('bdv', 'Banco de Venezuela')],
        ondelete={'bdv': 'set default'}
    )

    # =====================================================
    # CAMPOS ESPECÍFICOS DEL BANCO DE VENEZUELA
    # =====================================================
    bdv_api_url = fields.Char(
        string="URL API BDV (QA)",
        help="Endpoint de la API de conciliación del BDV para ambiente QA.",
        default="https://bdvconciliacionqa.banvenez.com:444/getMovement/v2",
    )
    bdv_api_url_prod = fields.Char(
        string="URL API BDV (Producción)",
        help="Endpoint de la API de conciliación del BDV para ambiente Producción.",
        default="https://bdvconciliacion.banvenez.com/getMovement",
    )

    bdv_environment = fields.Selection(
        selection=[
            ('qa', 'QA (Pruebas)'),
            ('prod', 'Producción'),
        ],
        string="Entorno BDV",
        default='qa',
        help="Define si se usa el ambiente de pruebas o producción del BDV.",
    )

    bdv_test_date = fields.Char(
        string="Fecha de Prueba QA",
        help="Fecha fija para pruebas en ambiente QA (formato: YYYY-MM-DD). Dejar vacío para usar fecha actual.",
        default="2023-02-12",
    )

    # =====================================================
    # MÉTODOS AUXILIARES
    # =====================================================
    def bdv_get_api_credentials(self, partner=None):
        """
        Devuelve las credenciales configuradas para el BDV.
        La API Key viene del partner según el ambiente (QA o Producción).
        El teléfono destino viene del partner (comercio).
        :param partner: res.partner record (el comercio/cliente)
        :return: dict con las credenciales
        """
        self.ensure_one()
        
        # Teléfono destino del partner (comercio) - siempre el mismo
        telefono_destino = ''
        if partner:
            telefono_destino = getattr(partner, 'bdv_telefono_destino', '') or ''
        
        # Seleccionar API Key y URL según el ambiente configurado en el provider
        if self.bdv_environment == 'prod':
            # PRODUCCIÓN: usar URL de producción y API Key de producción del partner
            api_key = getattr(partner, 'bdv_api_key_prod', '') if partner else ''
            api_url = 'https://bdvconciliacion.banvenez.com/getMovement'
        else:
            # QA: usar URL de QA y API Key de QA del partner
            api_key = getattr(partner, 'bdv_api_key', '') if partner else ''
            api_url = self.bdv_api_url or 'https://bdvconciliacionqa.banvenez.com:444/getMovement/v2'
        
        return {
            'api_key': api_key,
            'api_url': api_url,
            'telefono_destino': telefono_destino,
            'environment': self.bdv_environment or 'qa',
            'test_date': self.bdv_test_date or '',
        }
        # =====================================================
    # API CONSULTA DE MOVIMIENTOS
    # =====================================================
    def bdv_consultar_movimientos(self, cuenta, fecha_ini, fecha_fin, nro_movimiento='', partner=None):
        """
        Consulta movimientos de una cuenta en el BDV.
        """
        self.ensure_one()
        import requests

        # 1. Leemos el entorno directamente del provider
        env_type = self.bdv_environment.strip().lower() if self.bdv_environment else 'qa'

        if env_type == 'qa':
            api_key = '256D0FDD36F1B1B3F1208A9B6EC693'
            url = 'https://bdvconciliacionqa.banvenez.com:444/apis/bdv/consulta/movimientos/v2'
            
            # 🔥 PAYLOAD EXACTO SEGÚN LA DOCUMENTACIÓN ACTUALIZADA (Pág. 3 del nuevo PDF)
            # El banco cambió la cuenta de prueba dummy a "01029999999999999999"
            payload = {
                "cuenta": "01029999999999999999",  # <-- CAMBIO CRÍTICO: Nueva cuenta dummy
                "fechaIni": "01/01/2025",
                "fechaFin": "28/01/2025",
                "tipoMoneda": "VES",
                "nroMovimiento": ""  # Se mantiene como string vacío para la 1ra consulta, tal cual el PDF
            }
            _logger.info(f"[BDV MOVIMIENTOS] 🟢 QA: Usando payload EXACTO de la documentación actualizada (Cuenta: 01029999999999999999).")
            
        else:
            # PRODUCCIÓN: Usar la API Key general de producción (la misma para todas las APIs)
            api_key = getattr(partner, 'bdv_api_key_prod', '') if partner else ''
            if not api_key:
                return {
                    'success': False, 
                    'code': '9999', 
                    'message': 'Falta API Key de Producción en el comercio.'
                }
            
            # ✅ URL CORRECTA de producción según documentación oficial
            url = 'https://bdvconciliacion.banvenez.com/apis/bdv/consulta/movimientos'
            
            payload = {
                "cuenta": cuenta,
                "fechaIni": fecha_ini,
                "fechaFin": fecha_fin,
                "tipoMoneda": "VES",
            }
            if nro_movimiento:
                payload["nroMovimiento"] = nro_movimiento
            
            _logger.info(f"[BDV MOVIMIENTOS] 🔵 PRODUCCIÓN: Consultando cuenta {cuenta}")

        headers = {
            "X-API-Key": api_key, 
            "Content-Type": "application/json"
        }
        
        _logger.info(f"[BDV MOVIMIENTOS] Enviando petición a: {url}")
        _logger.info(f"[BDV MOVIMIENTOS] Payload: {payload}")
        
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=30)
            
            # Si hay error HTTP, leemos la respuesta REAL del banco
            if not response.ok:
                _logger.error(f"🚨 [BDV MOVIMIENTOS] Error HTTP {response.status_code}. Respuesta REAL: {response.text}")
                return {
                    'success': False, 
                    'code': str(response.status_code), 
                    'message': f'El banco rechazó la petición ({response.status_code}): {response.text}'
                }
            
            respuesta = response.json()
            
            if respuesta.get('code') == '1000':
                data = respuesta.get('data', {})
                return {
                    'success': True, 'code': '1000',
                    'total_of_movements': data.get('totalOfMovements', 0),
                    'movements': data.get('movs', []),
                    'has_more': data.get('totalOfMovements', 0) > 100,
                    'last_nro_movimiento': data.get('movs', [])[-1].get('nroMov') if data.get('movs') else '',
                }
            else:
                return {'success': False, 'code': respuesta.get('code'), 'message': respuesta.get('message')}
                
        except Exception as e:
            _logger.error(f"[BDV MOVIMIENTOS] Error de conexión inesperado: {str(e)}")
            return {'success': False, 'code': '9999', 'message': f'Error de conexión: {str(e)}'}