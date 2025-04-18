from odoo import models, api, _

class ProductTemplate(models.Model):
    _inherit = 'product.template'

    def open_configurator_wizard(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Configure Product'),
            'res_model': 'product.configurator.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_product_tmpl_id': self.id},
        }
