{
    'name': 'BestPay - Banco Mercantil',
    'version': '19.0.1.0.0',
    'category': 'Accounting/Payment',
    'summary': 'Integración con Banco Mercantil para BestPay',
    'author': 'Steam Solution',
    'depends': [
        'payment',
        'bestpay',
        'bestpay_mer',
    ],
    'data': [
        'security/ir.model.access.csv',
        'data/payment_processing_page.xml',
        'data/payment_provider_data.xml',
        'views/res_partner_views.xml',
        'views/payment_transaction_views.xml',
    ],
    'external_dependencies': {
        'python': ['Crypto'],
    },
    'installable': True,
    'application': False,
    'auto_install': False,
    'license': 'LGPL-3',
}