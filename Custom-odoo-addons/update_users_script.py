env = self.env if 'self' in locals() else env

# Fetch all existing users (you can add domain filters here if you only want specific users)
# By default, Odoo includes Superuser (ID 1) and other active users.
users = env['res.users'].search([])

for user in users:
    # Update the boolean flag if you meant `is_company_owner`
    user.write({
        'is_company_owner': True,
        # If you also meant to set the company_roleSelection field, uncomment the line below:
        # 'company_role': 'admin' # Could be 'super_admin', 'admin', or 'operator'
    })

env.cr.commit()
print(f"Successfully updated {len(users)} users.")
