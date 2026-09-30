# -*- coding: utf-8 -*-
"""
Utilidades de cifrado AES-256-GCM para BestPay.

Uso:
    from ..lib.crypto_utils import encrypt_payload, decrypt_payload, generate_key
    
    # Cifrar
    encrypted = encrypt_payload({"amount": 100}, "clave-base64")
    
    # Descifrar
    plain = decrypt_payload(encrypted, "clave-base64")
"""

import base64
import json
import os
import logging
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidTag

_logger = logging.getLogger(__name__)

# Constantes
PROTOCOL_VERSION = 1
ALGORITHM = 'aes-256-gcm'
IV_BYTES = 12      # 96 bits (recomendado para GCM)
KEY_BYTES = 32     # 256 bits (AES-256)


def generate_key() -> str:
    """
    Genera una nueva clave AES-256 aleatoria.
    Retorna: string base64 de 32 bytes (44 caracteres).
    
    Uso: Llama esto cuando creas un nuevo partner en BestPay
    y muéstrale la clave para que la copie a su sistema.
    """
    raw_key = os.urandom(KEY_BYTES)
    return base64.b64encode(raw_key).decode('ascii')


def _b64_encode(data: bytes) -> str:
    """Codifica bytes a base64 estándar."""
    return base64.b64encode(data).decode('ascii')


def _b64_decode(data: str) -> bytes:
    """Decodifica base64 a bytes."""
    return base64.b64decode(data)


def encrypt_payload(plaintext: dict, key_b64: str, key_id: str = 'v1') -> dict:
    """
    Cifra un dict usando AES-256-GCM.
    
    Args:
        plaintext: dict a cifrar (ej: {"amount": 100, "ref": "ORD-001"})
        key_b64: clave en base64 (44 caracteres)
        key_id: identificador de versión (default: "v1")
    
    Returns:
        dict con estructura:
        {
            "v": 1,
            "alg": "aes-256-gcm",
            "kid": "v1",
            "iv": "<base64>",
            "tag": "<base64>",
            "ct": "<base64>"
        }
    
    Raises:
        ValueError: si la clave es inválida
    """
    try:
        key_bytes = _b64_decode(key_b64)
    except Exception as e:
        raise ValueError(f"Clave base64 inválida: {e}")
    
    if len(key_bytes) != KEY_BYTES:
        raise ValueError(
            f"Clave inválida: se esperan {KEY_BYTES} bytes (AES-256), "
            f"se obtuvieron {len(key_bytes)}"
        )

    aesgcm = AESGCM(key_bytes)
    iv = os.urandom(IV_BYTES)
    
    # Serializar a JSON compacto
    data = json.dumps(
        plaintext, 
        separators=(',', ':'), 
        ensure_ascii=False
    ).encode('utf-8')
    
    # AESGCM.encrypt retorna ciphertext + tag concatenados
    ciphertext_and_tag = aesgcm.encrypt(iv, data, associated_data=None)
    ciphertext = ciphertext_and_tag[:-16]  # Todo menos los últimos 16 bytes
    tag = ciphertext_and_tag[-16:]          # Últimos 16 bytes (tag GCM)
    
    return {
        'v': PROTOCOL_VERSION,
        'alg': ALGORITHM,
        'kid': key_id,
        'ct': _b64_encode(ciphertext),
        'iv': _b64_encode(iv),
        'tag': _b64_encode(tag),
    }


def decrypt_payload(encrypted_payload: dict, key_b64: str) -> dict:
    """
    Descifra un payload cifrado con AES-256-GCM.
    
    Args:
        encrypted_payload: dict con claves v, alg, ct, iv, tag
        key_b64: clave en base64 (44 caracteres)
    
    Returns:
        dict descifrado
    
    Raises:
        ValueError: si el formato es inválido
        InvalidTag: si la autenticación falla (clave incorrecta o datos alterados)
    """
    required = {'v', 'alg', 'ct', 'iv', 'tag'}
    missing = required - set(encrypted_payload.keys())
    if missing:
        raise ValueError(f"Payload cifrado incompleto. Faltan: {missing}")
    
    if encrypted_payload.get('v') != PROTOCOL_VERSION:
        raise ValueError(
            f"Versión de protocolo no soportada: {encrypted_payload.get('v')}"
        )
    
    if encrypted_payload.get('alg') != ALGORITHM:
        raise ValueError(
            f"Algoritmo no soportado: {encrypted_payload.get('alg')}"
        )
    
    try:
        key_bytes = _b64_decode(key_b64)
        iv = _b64_decode(encrypted_payload['iv'])
        ciphertext = _b64_decode(encrypted_payload['ct'])
        tag = _b64_decode(encrypted_payload['tag'])
    except Exception as e:
        raise ValueError(f"Error decodificando base64: {e}")
    
    aesgcm = AESGCM(key_bytes)
    # Reconstruir ciphertext + tag como espera la librería
    ciphertext_and_tag = ciphertext + tag
    
    try:
        plaintext_bytes = aesgcm.decrypt(
            iv, 
            ciphertext_and_tag, 
            associated_data=None
        )
    except InvalidTag:
        _logger.warning("Fallo de autenticación AES-GCM (tag inválido)")
        raise
    
    return json.loads(plaintext_bytes.decode('utf-8'))


def is_encrypted_payload(body: dict) -> bool:
    """
    Detecta si un dict es un payload cifrado válido.
    No lanza excepciones, solo retorna True/False.
    
    Uso: Para saber si necesitas descifrar el body recibido.
    """
    if not isinstance(body, dict):
        return False
    return all(k in body for k in ('v', 'alg', 'ct', 'iv', 'tag'))