# -*- coding: utf-8 -*-
from odoo import models, fields, api
import requests
from bs4 import BeautifulSoup
import logging

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
                
                # DIAGNÓSTICO: Ver qué hay en la página
                all_divs = soup.find_all('div')
                _logger.info(f'[BCV SYNC] Total de divs encontrados: {len(all_divs)}')
                
                # Mostrar los primeros 20 IDs de divs
                divs_with_id = [div.get('id') for div in all_divs if div.get('id')]
                _logger.info(f'[BCV SYNC] IDs de divs: {divs_with_id[:20]}')
                
                # Buscar el dólar
                section_dolar = soup.find('div', id='dolar')
                section_euro = soup.find('div', id='euro')
                
                _logger.info(f'[BCV SYNC] Contenedor dólar encontrado: {section_dolar is not None}')
                _logger.info(f'[BCV SYNC] Contenedor euro encontrado: {section_euro is not None}')
                
                if section_dolar:
                    try:
                        texto_dolar = section_dolar.find('strong').text.strip()
                        tasa_dolar = float(texto_dolar.replace(',', '.'))
                        
                        currency_ves = self.env['res.currency'].search([('name', '=', 'VES')], limit=1)
                        if currency_ves:
                            self.env['res.currency.rate'].create({
                                'currency_id': currency_ves.id,
                                'name': fields.Datetime.now(),
                                'rate': tasa_dolar,
                            })
                            _logger.info(f"[BCV SYNC] ✓✓ Dólar actualizado: {tasa_dolar} Bs/USD")
                    except Exception as e:
                        _logger.error(f"[BCV SYNC] Error procesando dólar: {str(e)}")
                else:
                    _logger.warning("[BCV SYNC] ✗ Contenedor dólar NO encontrado")
                
                if section_euro:
                    try:
                        texto_euro = section_euro.find('strong').text.strip()
                        tasa_euro = float(texto_euro.replace(',', '.'))
                        
                        if tasa_dolar and tasa_euro > 0:
                            currency_eur = self.env['res.currency'].search([('name', '=', 'EUR')], limit=1)
                            if currency_eur:
                                tasa_final_eur = tasa_dolar / tasa_euro
                                self.env['res.currency.rate'].create({
                                    'currency_id': currency_eur.id,
                                    'name': fields.Datetime.now(),
                                    'rate': tasa_final_eur,
                                })
                                _logger.info(f"[BCV SYNC] ✓✓ Euro actualizado: {tasa_final_eur} EUR/USD")
                    except Exception as e:
                        _logger.error(f"[BCV SYNC] Error procesando euro: {str(e)}")
                else:
                    _logger.warning("[BCV SYNC] ✗ Contenedor euro NO encontrado")
                    
        except Exception as e:
            _logger.error(f"[BCV SYNC] Excepción crítica: {str(e)}", exc_info=True)
        
        _logger.info('[BCV SYNC] ========== FIN ACTUALIZACIÓN ==========')