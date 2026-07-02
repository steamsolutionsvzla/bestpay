# -*- coding: utf-8 -*-
from odoo import models, fields, api
import requests
from bs4 import BeautifulSoup
import logging

_logger = logging.getLogger(__name__)

# 1. EXTENDEMOS LA TABLA DE TASAS PARA AGREGAR NUESTRO CAMPO PROPIO DE BESTPAY
class ResCurrencyRate(models.Model):
    _inherit = 'res.currency.rate'

    # Campo donde guardaremos el valor limpio del BCV (ej. 639.70) sin alterar la contabilidad nativa
    bcv_rate_real = fields.Float(string='Tasa Real BCV (BestPay)', digits=(12, 4))


class ResCurrency(models.Model):
    _inherit = 'res.currency'

    @api.model
    def _update_bcv_rate(self):
        _logger.info('[BCV SYNC] INICIANDO EXTRACCIÓN PARA PASARELA BESTPAY (BASE VES)')
        
        url = "https://www.bcv.org.ve/"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }

        try:
            response = requests.get(url, headers=headers, verify=False, timeout=30)
            if response.status_code != 200:
                _logger.warning(f"[BCV SYNC] No se pudo conectar al sitio del BCV. Status: {response.status_code}")
                return

            soup = BeautifulSoup(response.content, 'html.parser')
            today = fields.Date.today()
            
            monedas_bcv = {
                'USD': 'dolar',
                'EUR': 'euro'
            }

            for name, html_id in monedas_bcv.items():
                section = soup.find('div', id=html_id)
                if not section:
                    continue

                texto = section.find('strong').text.strip()
                tasa_real_bcv = float(texto.replace(',', '.'))
                
                if tasa_real_bcv <= 0:
                    continue

                # Odoo Base VES: rate contable obligatorio nativo = 1 / tasa_bcv
                rate_odoo_contable = 1.0 / tasa_real_bcv

                currency = self.env['res.currency'].search([('name', '=', name)], limit=1)
                if not currency:
                    continue

                # Limpieza de registros previos del día actual para evitar colisiones
                existing_rates = self.env['res.currency.rate'].search([
                    ('currency_id', '=', currency.id),
                    ('name', '=', today),
                    ('company_id', '=', self.env.company.id)
                ])
                if existing_rates:
                    existing_rates.unlink()

                # Creamos el registro inyectando el valor inverso en el nativo,
                # y el valor que tú necesitas de 639.70 en tu campo de Bestpay.
                self.env['res.currency.rate'].create({
                    'currency_id': currency.id,
                    'name': today,
                    'rate': rate_odoo_contable,               # Odoo contabilidad nativa (0.00156)
                    'bcv_rate_real': tasa_real_bcv,           # Tu data para la Pasarela de pagos (639.70)
                    'company_id': self.env.company.id,
                })
                
                _logger.info(f'[BCV SYNC] ✓ {name} Sincronizado. Contable: {rate_odoo_contable} | BestPay Real: {tasa_real_bcv}')

            # Invalida caché para persistencia inmediata en la interfaz
            self.env.invalidate_all()

        except Exception as e:
            _logger.error(f"[BCV SYNC] Error en extracción: {str(e)}", exc_info=True)