from odoo import http
from odoo.http import request

class DataImporterController(http.Controller):

    @http.route('/data_importer/download_template/<int:importer_id>', type='http', auth='user')
    def download_template(self, importer_id):
        importer = request.env['data.importer'].browse(importer_id).sudo()
        headers = importer._get_template_headers()

        csv_content = ','.join(headers) + "\n"
        filename = '%s_template.csv' % (importer.model_id.model.replace('.', '_'),)

        return request.make_response(
            csv_content,
            headers=[
                ('Content-Type', 'text/csv'),
                ('Content-Disposition', f'attachment; filename="{filename}"'),
            ]
        )
