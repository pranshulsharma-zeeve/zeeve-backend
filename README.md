# Odoo Test

## Zoho Subscription Migration Playbook

The `data_importer` module contains the Zoho → Odoo migration helpers that now support multiple nodes per subscription and conditional Stripe creation. Share the following checklist with anyone preparing or auditing a migration run.

1. **Prepare the CSV.**
   - Required headers: `Subscription ID`, `Subscription#`, `Customer Email`, `Customer Name`, `Item Code`, `Item Price`, `Quantity`, `Status`, `Start Date`, `Next Billing Date`, `Currency Code`, `Node Metadata`.
   - The optional *Node Metadata* column must contain JSON (`{"nodes": [{...}]}` or a single dict) with per-node fields such as `node_name`, `node_identifier`, `network`, `server_location`, `software_update_rule`, `metadata`, and `endpoint`.
   - See `Custom-odoo-addons/data_importer/sample_subscription_import.csv` for a ready-to-use template that covers active rows (new Stripe subscription), duplicate rows (quantity increase), and suspended rows (Odoo-only).

2. **Primary keys and matching.**
   - The importer treats Zoho's `Subscription ID` as the canonical identifier and stores it in both `subscription.subscription.subscription_uuid` and `zoho_subscription_id`.
   - When the same Zoho ID already exists in Odoo, the importer compares **plan**, **protocol**, and **payment frequency** (parsed from `Item Code`). If all three match, the existing subscription is updated (quantity and total price increase) and the new node records are appended instead of creating a duplicate subscription.
   - If the Zoho ID exists but the plan/protocol/frequency combination differs, a brand new subscription is created so that each unique offering remains separate.

3. **Node creation.**
   - Every row produces one or more `subscription.node` records. When `Node Metadata` contains an array, all entries are created and linked to the subscription. Missing metadata falls back to the row-level defaults (network, software update rule, and Zoho node identifier).
   - Node and subscription states are mapped from Zoho's `Status` column (e.g., `active/live` → `ready`, `cancelled` → `closed`, `trial` → `syncing`) so the downstream APIs can rely on consistent enums.

4. **Stripe creation policy.**
   - Only rows whose Zoho `Status` is **Active** trigger Stripe subscription creation. Every other status (trial, cancelled, suspended, etc.) results in an Odoo-only subscription; the row still succeeds and the log explicitly notes that Stripe was skipped intentionally.
   - If Zoho marks a row as Active but the Stripe customer is missing or lacks a reusable payment method, the importer still creates the Odoo subscription, records the Stripe failure message next to the subscription ID, and returns the row as a “partial” success so the operator can retry later.

5. **Error handling & logging.**
   - Customer lookups, plan mapping, and Stripe API calls are wrapped in savepoints; a failure on one row never aborts the entire migration.
   - Every outcome (success, partial, skipped) is written to the importer log with the subscription ID and any accumulated warnings (e.g., “Stripe error for subscription SUB-123: ...”). This satisfies the requirement to capture failures without halting the script.
   - Because each row runs inside its own transaction, partially imported rows (Odoo record created but no Stripe subscription) can be reprocessed safely after fixing the upstream issue.

6. **Running the migration.**
   - In Odoo, open **Data Importer → New Import**, select the model `subscription.subscription`, upload the CSV, and start the import. The summary modal will highlight how many rows created subscriptions only versus subscriptions plus Stripe records.
   - After the import, review the log attached to the Data Importer record to spot any rows that should be retried (look for “partial” lines). The log already includes the Zoho Subscription ID so customer success can reconcile discrepancies.



## Getting started

To make it easy for you to get started with GitLab, here's a list of recommended next steps.

Already a pro? Just edit this README.md and make it your own. Want to make it easy? [Use the template at the bottom](#editing-this-readme)!

## Add your files

- [ ] [Create](https://docs.gitlab.com/ee/user/project/repository/web_editor.html#create-a-file) or [upload](https://docs.gitlab.com/ee/user/project/repository/web_editor.html#upload-a-file) files
- [ ] [Add files using the command line](https://docs.gitlab.com/topics/git/add_files/#add-files-to-a-git-repository) or push an existing Git repository with the following command:

```
cd existing_repo
git remote add origin https://code.zeeve.net/zeeve/odoo-test.git
git branch -M main
git push -uf origin main
```

## Integrate with your tools

- [ ] [Set up project integrations](https://code.zeeve.net/zeeve/odoo-test/-/settings/integrations)

## Collaborate with your team

- [ ] [Invite team members and collaborators](https://docs.gitlab.com/ee/user/project/members/)
- [ ] [Create a new merge request](https://docs.gitlab.com/ee/user/project/merge_requests/creating_merge_requests.html)
- [ ] [Automatically close issues from merge requests](https://docs.gitlab.com/ee/user/project/issues/managing_issues.html#closing-issues-automatically)
- [ ] [Enable merge request approvals](https://docs.gitlab.com/ee/user/project/merge_requests/approvals/)
- [ ] [Set auto-merge](https://docs.gitlab.com/user/project/merge_requests/auto_merge/)

## Test and Deploy

Use the built-in continuous integration in GitLab.

- [ ] [Get started with GitLab CI/CD](https://docs.gitlab.com/ee/ci/quick_start/)
- [ ] [Analyze your code for known vulnerabilities with Static Application Security Testing (SAST)](https://docs.gitlab.com/ee/user/application_security/sast/)
- [ ] [Deploy to Kubernetes, Amazon EC2, or Amazon ECS using Auto Deploy](https://docs.gitlab.com/ee/topics/autodevops/requirements.html)
- [ ] [Use pull-based deployments for improved Kubernetes management](https://docs.gitlab.com/ee/user/clusters/agent/)
- [ ] [Set up protected environments](https://docs.gitlab.com/ee/ci/environments/protected_environments.html)

***

# Editing this README

When you're ready to make this README your own, just edit this file and use the handy template below (or feel free to structure it however you want - this is just a starting point!). Thanks to [makeareadme.com](https://www.makeareadme.com/) for this template.

## Suggestions for a good README

Every project is different, so consider which of these sections apply to yours. The sections used in the template are suggestions for most open source projects. Also keep in mind that while a README can be too long and detailed, too long is better than too short. If you think your README is too long, consider utilizing another form of documentation rather than cutting out information.

## Name
Choose a self-explaining name for your project.

## Description
Let people know what your project can do specifically. Provide context and add a link to any reference visitors might be unfamiliar with. A list of Features or a Background subsection can also be added here. If there are alternatives to your project, this is a good place to list differentiating factors.

## Badges
On some READMEs, you may see small images that convey metadata, such as whether or not all the tests are passing for the project. You can use Shields to add some to your README. Many services also have instructions for adding a badge.

## Visuals
Depending on what you are making, it can be a good idea to include screenshots or even a video (you'll frequently see GIFs rather than actual videos). Tools like ttygif can help, but check out Asciinema for a more sophisticated method.

## Installation
Within a particular ecosystem, there may be a common way of installing things, such as using Yarn, NuGet, or Homebrew. However, consider the possibility that whoever is reading your README is a novice and would like more guidance. Listing specific steps helps remove ambiguity and gets people to using your project as quickly as possible. If it only runs in a specific context like a particular programming language version or operating system or has dependencies that have to be installed manually, also add a Requirements subsection.

## Usage
Use examples liberally, and show the expected output if you can. It's helpful to have inline the smallest example of usage that you can demonstrate, while providing links to more sophisticated examples if they are too long to reasonably include in the README.

## Support
Tell people where they can go to for help. It can be any combination of an issue tracker, a chat room, an email address, etc.

## Roadmap
If you have ideas for releases in the future, it is a good idea to list them in the README.

## Contributing
State if you are open to contributions and what your requirements are for accepting them.

For people who want to make changes to your project, it's helpful to have some documentation on how to get started. Perhaps there is a script that they should run or some environment variables that they need to set. Make these steps explicit. These instructions could also be useful to your future self.

You can also document commands to lint the code or run tests. These steps help to ensure high code quality and reduce the likelihood that the changes inadvertently break something. Having instructions for running tests is especially helpful if it requires external setup, such as starting a Selenium server for testing in a browser.

## Authors and acknowledgment
Show your appreciation to those who have contributed to the project.

## License
For open source projects, say how it is licensed.

## Project status
If you have run out of energy or time for your project, put a note at the top of the README saying that development has slowed down or stopped completely. Someone may choose to fork your project or volunteer to step in as a maintainer or owner, allowing your project to keep going. You can also make an explicit request for maintainers.
