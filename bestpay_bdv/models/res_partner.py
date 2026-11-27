# -*- coding: utf-8 -*-
import json
import logging
from odoo import api, fields, models
from odoo.exceptions import UserError

from odoo.addons.bestpay.utils.identity import phone_variants

_logger = logging.getLogger(__name__)


class ResPartnerBDV(models.Model):
    _inherit = 'res.partner'

    # Clave de respaldo de QA que el banco documentó para pruebas.
    BDV_QA_NOTIFICATION_KEY = '97F6F54EF1A84F3A24FE19A3B338C77A'

    # =====================================================
    # CAMPOS ESPECÍFICOS DEL BANCO DE VENEZUELA (Por Comercio)
    # =====================================================
    # Estos campos antes vivían en el payment.provider, pero como son
    # propios de cada comercio (la API Key la da el banco al comercio
    # y el teléfono destino es donde el comercio recibe los pagos),
    # deben vivir en el partner (cliente).

    bdv_api_key = fields.Char(
    string="API Key BDV (QA)",
    help="Clave de autenticación del ambiente de calidad (dummy) proporcionada por el BDV.",
    groups="base.group_system",
    copy=False,
    )
    bdv_api_key_prod = fields.Char(
        string="API Key BDV (Producción)",
        help="Clave de autenticación del ambiente de producción (generada desde BDVenlínea Empresas).",
        groups="base.group_system",
        copy=False,
    )

    bdv_telefono_destino = fields.Char(
        string="Teléfono Destino BDV (Comercio)",
        help="Número de teléfono al que este comercio recibe los pagos móviles (formato: 04XXXXXXXXX).",
        copy=False,
    )
    
    bdv_telefono_destino_qa = fields.Char(
    string="Teléfono Destino BDV (QA)",
    help="Número de teléfono al que este comercio recibe los pagos móviles en QA/Pruebas (formato: 04XXXXXXXXX).",
    copy=False,
    )

    # C2P

    bdv_api_key_c2p = fields.Char(
        string="API Key C2P BDV",
        help="API Key específica para el flujo C2P del Banco de Venezuela"
    )
    
    bdv_phone_destino_c2p = fields.Char(
        string="Teléfono/Cuenta Destino C2P",
        help="Número de teléfono o cuenta destino del comercio para recibir pagos C2P"
    )

        # === API NOTIFICACIÓN ===
    bdv_api_key_notification = fields.Char(
        string="API Key Notificación BDV",
        help="API Key para recibir notificaciones automáticas de pagos del BDV. "
             "En QA usar: 97F6F54EF1A84F3A24FE19A3B338C77A",
        groups="base.group_system",
        copy=False,
    )

    bdv_telefono_notificacion = fields.Char(
        string="Teléfono para Notificación BDV",
        help="Teléfono que el BDV usará para enviar notificaciones webhook. "
            "En QA usar: 04141234567",
        copy=False,
    )

    # === API CONSULTA DE MOVIMIENTOS ===
    bdv_api_key_movimientos = fields.Char(
        string="API Key Consulta Movimientos BDV",
        help="API Key para consultar movimientos de cuenta en el BDV. "
             "En QA usar: 256D0FDD36F1B1B3F1208A9B6EC693",
        groups="base.group_system",
        copy=False,
    )

    # =====================================================
    # RESOLUCIÓN DEL COMERCIO/CLIENTE PARA NOTIFICACIONES
    # =====================================================
    def _bdv_notification_key(self, env_type='qa'):
        """API key de notificación del partner según el ambiente."""
        self.ensure_one()
        if (env_type or 'qa').strip().lower() == 'prod':
            return str(self.bdv_api_key_prod or '').strip()
        return str(self.bdv_api_key_notification or '').strip()

    @api.model
    def _bdv_find_notification_partners(self, numero_comercio, api_key='', env_type='qa'):
        """Partners candidatos para un aviso del BDV.

        Combina dos señales:
        - el teléfono del comercio (puede apuntar a uno o varios clientes), y
        - la API key del webhook (identifica la cuenta aunque el teléfono esté
          mal configurado).

        Devolver varios partners es intencional: en una cuenta compartida el
        teléfono no alcanza para decidir. El motor de casado usa la
        transacción para elegir el partner correcto.
        """
        partners = self.browse()
        phone = str(numero_comercio or '').strip()
        if phone:
            # Acepta el teléfono tal cual o con formatos equivalentes
            # (0416..., 416..., +58 416...).
            variants = set(phone_variants(phone))
            variants.add(phone)
            partners = self.sudo().search([
                '|',
                ('bdv_telefono_destino', 'in', list(variants)),
                ('bdv_telefono_notificacion', 'in', list(variants)),
            ])

        key = str(api_key or '').strip()
        if key:
            key_field = (
                'bdv_api_key_prod'
                if (env_type or 'qa').strip().lower() == 'prod'
                else 'bdv_api_key_notification'
            )
            partners |= self.sudo().search([(key_field, '=', key)])

        return partners

    @api.model
    def _bdv_notification_key_is_valid(self, partners, api_key, env_type='qa'):
        """Valida la API key del webhook contra los candidatos.

        Si algún candidato tiene una key configurada, la recibida debe coincidir
        con alguna. Si ninguno tiene key (comercio nuevo o QA sin configurar),
        se mantiene el fallback histórico de QA.
        """
        api_key = str(api_key or '').strip()
        if not api_key:
            return False

        configured_keys = []
        for partner in partners:
            key = partner._bdv_notification_key(env_type)
            if key:
                configured_keys.append(key)

        if configured_keys:
            return api_key in configured_keys

        return (
            (env_type or 'qa').strip().lower() != 'prod'
            and api_key == self.BDV_QA_NOTIFICATION_KEY
        )


        # =================================================================
    # 🔔 CONFIGURACIÓN DE WEBHOOK AL TERCERO (BestPay → Koole/ecommerce)
    # =================================================================
    # TODO [MIGRACIÓN FASE 7]: Mover estos 3 campos al módulo base 'bestpay'
    # para que sean compartidos por BDV y Mercantil. Al hacerlo, eliminarlos
    # de aquí y ajustar las vistas. Wilson ya tiene nombres similares en
    # 'bestpay_mer/models/res_partner.py' que también deberían migrarse.
    webhook_url_3ro = fields.Char(
        string="URL Webhook del Tercero",
        help="URL del sistema del tercero (ej. Koole) donde BestPay notificará "
             "de forma asíncrona cuando el pago sea confirmado por el banco.",
        copy=False,
    )
    default_return_url_3ro = fields.Char(
        string="URL de Retorno por Defecto (3ro)",
        help="URL a la que el checkout redirigirá al pagador tras completar "
             "el flujo (éxito/error). Se usará en la Fase 5.",
        copy=False,
    )
    bestpay_webhook_active = fields.Boolean(
        string="Webhook Activo",
        default=True,
        help="Desmarca para suspender los envíos de webhook a este tercero.",
    )

        # =====================================================
    # 🎨 DATOS DE PRESENTACIÓN AL CLIENTE FINAL (CHECKOUT BDV) CONCILIACION
    # =====================================================
    bestpay_checkout_title = fields.Char(
        string="Título del Checkout",
        help="Nombre comercial que se muestra al pagador en la página de pago. Si está vacío, se usa el nombre del contacto.",
    )
    bestpay_checkout_message = fields.Html(
        string="Mensaje de Instrucciones",
        help="Mensaje personalizado (HTML) que aparece antes del formulario. Ej: 'Realice el Pago Móvil y luego registre los datos abajo.'",
    )
    bestpay_checkout_primary_color = fields.Char(
        string="Color de Marca",
        default="#0033a0",  # Azul BDV por defecto para que combine con tu CSS
        help="Color hexadecimal principal para el checkout (bordes, detalles, etc).",
    )

    # =====================================================
    #  DATOS DE PAGO MÓVIL (Para mostrar en checkout)
    # =====================================================
    bestpay_pagomovil_telefono = fields.Char(
        string="Teléfono Pago Móvil",
        help="Número de teléfono para recibir pagos móviles (se mostrará en el checkout).",
    )
    bestpay_pagomovil_rif = fields.Char(
        string="RIF Pago Móvil",
        help="RIF del comercio para pagos móviles (se mostrará en el checkout).",
    )
    bestpay_pagomovil_banco = fields.Char(
        string="Banco Pago Móvil",
        help="Banco del comercio para pagos móviles (ej: Banco de Venezuela).",
    )

    bestpay_pagomovil_qr = fields.Binary(
    string="QR Pago Móvil",
    attachment=True,
    help="Imagen del QR generado desde la app del Banco de Venezuela. "
         "Se mostrará al cliente en el checkout para que escanee y realice el pago."
    )

     # =====================================================
    # CAMPO COMPUTADO: Entorno BDV (para vistas)
    # =====================================================
    # Este campo lee el ambiente del provider BDV activo para poder
    # ocultar los campos de QA cuando el provider esté en producción.
    # En producción, todas las APIs usan la misma key (bdv_api_key_prod),
    # así que los campos específicos de QA no tienen sentido mostrarlos.
    bdv_environment = fields.Selection(
        selection=[
            ('qa', 'QA (Pruebas)'),
            ('prod', 'Producción'),
        ],
        string="Entorno BDV (Computado)",
        compute='_compute_bdv_environment',
        help="Ambiente configurado en el provider BDV. Se usa para ocultar campos de QA en producción."
    )
    
    @api.depends('allowed_provider_ids')
    def _compute_bdv_environment(self):
        """Lee el ambiente del provider BDV activo"""
        for partner in self:
            provider = self.env['payment.provider'].sudo().search([
                ('code', '=', 'bdv'),
                ('is_bestpay_provider', '=', True),
            ], limit=1)
            partner.bdv_environment = provider.bdv_environment if provider else 'qa'
    # =================================================================
    # ⚠️ NOTA: MÉTODO TEMPORAL - Usa Wizard TransientModel
    # =================================================================
    # Este método abre un wizard intermedio para que el usuario ingrese
    # los datos de la consulta (cuenta, fechas). Luego ejecuta la consulta
    # con paginación automática y muestra los resultados en otro wizard.
    # =================================================================
    def action_bdv_consultar_movimientos(self):
        """Abre el wizard intermedio para ingresar datos de consulta"""
        self.ensure_one()
        
        # Validar que el provider BDV esté configurado
        provider = self.env['payment.provider'].sudo().search([
            ('code', '=', 'bdv'),
            ('is_bestpay_provider', '=', True),
        ], limit=1)
        
        if not provider:
            raise UserError("No hay proveedor BDV configurado en el sistema.")
        
        # Validar que haya API Key según el ambiente
        env_type = provider.bdv_environment.strip().lower() if provider.bdv_environment else 'qa'
        if env_type == 'qa':
            if not self.bdv_api_key_movimientos:
                raise UserError("Este comercio no tiene configurada la API Key de Movimientos (QA).")
        else:
            if not self.bdv_api_key_prod:
                raise UserError("Este comercio no tiene configurada la API Key de Producción.")
        
        # Abrir wizard intermedio
        return {
            'type': 'ir.actions.act_window',
            'name': f'Consultar Movimientos BDV - {self.name}',
            'res_model': 'bestpay_bdv.consulta.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_partner_id': self.id,
            }
        }