# -*- coding: utf-8 -*-
from odoo import models, fields, api
import requests
from bs4 import BeautifulSoup
import logging
from datetime import datetime

_logger = logging.getLogger(__name__)

class ResCurrency(models.Model):
    _inherit = 'res.currency'

    @api.model
    def _update_bcv_rate(self):
        _logger.info('[BCV SYNC] ========== INICIANDO ACTUALIZACIÓN ==========')
        
        url = "https://www.bcv.org.ve/"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }

        try:
            response = requests.get(url, headers=headers, verify=False, timeout=30)
            
            if response.status_code == 200:
                soup = BeautifulSoup(response.content, 'html.parser')
                
                section_dolar = soup.find('div', id='dolar')
                section_euro = soup.find('div', id='euro')
                
                _logger.info(f'[BCV SYNC] Contenedor dólar: {section_dolar is not None}')
                _logger.info(f'[BCV SYNC] Contenedor euro: {section_euro is not None}')
                
                # Procesar DÓLAR
                if section_dolar:
                    try:
                        texto_dolar = section_dolar.find('strong').text.strip()
                        tasa_dolar = float(texto_dolar.replace(',', '.'))
                        
                        currency_ves = self.env['res.currency'].search([('name', '=', 'VES')], limit=1)
                        if currency_ves:
                            today = fields.Date.today()
                            # Buscar si ya existe una tasa para hoy
                            rate_ves = self.env['res.currency.rate'].search([
                                ('currency_id', '=', currency_ves.id),
                                ('name', '=', today)
                            ], limit=1)
                            
                            if rate_ves:
                                # Actualizar la tasa existente
                                rate_ves.write({'rate': tasa_dolar})
                                _logger.info(f"[BCV SYNC] ✓ Dólar ACTUALIZADO: {tasa_dolar} Bs/USD")
                            else:
                                # Crear nueva tasa
                                self.env['res.currency.rate'].create({
                                    'currency_id': currency_ves.id,
                                    'name': today,
                                    'rate': tasa_dolar,
                                })
                                _logger.info(f"[BCV SYNC] ✓ Dólar CREADO: {tasa_dolar} Bs/USD")
                    except Exception as e:
                        _logger.error(f"[BCV SYNC] Error procesando dólar: {str(e)}")
                        self.env.cr.rollback()  # Importante: rollback si hay error
                
                # Procesar EURO (en una transacción separada)
                if section_euro:
                    try:
                        texto_euro = section_euro.find('strong').text.strip()
                        tasa_euro = float(texto_euro.replace(',', '.'))
                        
                        currency_eur = self.env['res.currency'].search([('name', '=', 'EUR')], limit=1)
                        if currency_eur and tasa_euro > 0:
                            today = fields.Date.today()
                            # Buscar si ya existe una tasa para hoy
                            rate_eur = self.env['res.currency.rate'].search([
                                ('currency_id', '=', currency_eur.id),
                                ('name', '=', today)
                            ], limit=1)
                            
                            if rate_eur:
                                # Actualizar la tasa existente
                                rate_eur.write({'rate': tasa_euro})
                                _logger.info(f"[BCV SYNC] ✓ Euro ACTUALIZADO: {tasa_euro} Bs/EUR")
                            else:
                                # Crear nueva tasa
                                self.env['res.currency.rate'].create({
                                    'currency_id': currency_eur.id,
                                    'name': today,
                                    'rate': tasa_euro,
                                })
                                _logger.info(f"[BCV SYNC] ✓ Euro CREADO: {tasa_euro} Bs/EUR")
                    except Exception as e:
                        _logger.error(f"[BCV SYNC] Error procesando euro: {str(e)}")
                        self.env.cr.rollback()
                        
        except Exception as e:
            _logger.error(f"[BCV SYNC] Excepción crítica: {str(e)}", exc_info=True)
        
        _logger.info('[BCV SYNC] ========== FIN ACTUALIZACIÓN ==========')