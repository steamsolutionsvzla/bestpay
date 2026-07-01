{
    'name': 'BestPay',
    'version': '19.0.1.0.0',
    'category': 'Accounting',
    'summary': 'Módulo de integración de pagos BestPay',
    'description': 'Verificador de pagos y enlaces de pago para Steam Solutions',
    'author': 'Samuel',

    'depends': [
        'base',
        'payment',
    ],
    'data': [
        'data/cron_bcv.xml',
        'data/bestpay_menus.xml',
        # 'security/ir.model.access.csv',
        'views/res_partner_views.xml',
        'views/payment_transaction_views.xml',
    ],

    'installable': True,
    'application': True,
    'auto_install': False,
    'license': 'LGPL-3',
}