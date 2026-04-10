#Manifest for the zeeve_base module.

{
    "name": "Zeeve Base",
    "version": "1.0",
    "sequence": 2,
    "author": "Your Company",
    "category": "Administration",
    "summary": "Administrative configuration for protocols and server locations",
    "depends": [
        "base",
        "web",
        "contacts",
        "product",
        "mail",
        "portal",
        "auth_signup",
        "utm",
        "sale_management",
        "spreadsheet_dashboard",

    ],
    "data": [
        "security/ir.model.access.csv",
        "data/mail_template.xml",
        "data/reports-template.xml",
        "data/report-email-cron.xml",
        "views/protocol_master_views.xml",
        "views/server_location_views.xml",
        "views/zeeve_config_views.xml",
        "views/zeeve_network_type_views.xml",
        "views/contact_us.xml",
        "views/menu_hide_views.xml",
        "views/menu_access.xml",
        "views/res_users_views.xml",
    ],
    "installable": True,
    "application": False,
}
