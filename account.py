#This file is part of Tryton.  The COPYRIGHT file at the top level of
#this repository contains the full copyright notices and license terms.
from decimal import Decimal
import copy
from trytond.model import ModelView, ModelSQL, fields
from trytond.wizard import Wizard
from trytond.pyson import Equal, Eval, Not, PYSONEncoder
from trytond.transaction import Transaction
from trytond.pool import Pool


class Account(ModelSQL, ModelView):
    'Analytic Account'
    _name = 'analytic_account.account'
    _description = __doc__

    name = fields.Char('Name', required=True, translate=True, select=1)
    code = fields.Char('Code', select=1)
    active = fields.Boolean('Active', select=2)
    company = fields.Many2One('company.company', 'Company')
    currency = fields.Many2One('currency.currency', 'Currency', required=True)
    currency_digits = fields.Function(fields.Integer('Currency Digits',
        on_change_with=['currency']), 'get_currency_digits')
    type = fields.Selection([
        ('root', 'Root'),
        ('view', 'View'),
        ('normal', 'Normal'),
        ], 'Type', required=True)
    root = fields.Many2One('analytic_account.account', 'Root', select=2,
            domain=[('parent', '=', False)],
            states={
                'invisible': Equal(Eval('type'), 'root'),
                'required': Not(Equal(Eval('type'), 'root')),
            })
    parent = fields.Many2One('analytic_account.account', 'Parent', select=2,
            domain=[('parent', 'child_of', Eval('root'))],
            states={
                'invisible': Equal(Eval('type'), 'root'),
                'required': Not(Equal(Eval('type'), 'root')),
            })
    childs = fields.One2Many('analytic_account.account', 'parent', 'Children')
    balance = fields.Function(fields.Numeric('Balance',
        digits=(16, Eval('currency_digits', 1)), depends=['currency_digits']),
        'get_balance')
    credit = fields.Function(fields.Numeric('Credit',
        digits=(16, Eval('currency_digits', 2)), depends=['currency_digits']),
        'get_credit_debit')
    debit = fields.Function(fields.Numeric('Debit',
        digits=(16, Eval('currency_digits', 2)), depends=['currency_digits']),
        'get_credit_debit')
    state = fields.Selection([
        ('draft', 'Draft'),
        ('opened', 'Opened'),
        ('closed', 'Closed'),
        ], 'State', required=True)
    note = fields.Text('Note')
    display_balance = fields.Selection([
        ('debit-credit', 'Debit - Credit'),
        ('credit-debit', 'Credit - Debit'),
        ], 'Display Balance', required=True)
    mandatory = fields.Boolean('Mandatory', states={
        'invisible': Not(Equal(Eval('type'), 'root')),
        })

    def __init__(self):
        super(Account, self).__init__()
        self._constraints += [
            ('check_recursion', 'recursive_accounts'),
        ]
        self._error_messages.update({
            'recursive_accounts': 'You can not create recursive accounts!',
        })
        self._order.insert(0, ('code', 'ASC'))

    def default_active(self):
        return True

    def default_company(self):
        return Transaction().context.get('company') or False

    def default_currency(self):
        company_obj = Pool().get('company.company')
        currency_obj = Pool().get('currency.currency')
        if Transaction().context.get('company'):
            company = company_obj.browse(Transaction().context['company'])
            return company.currency.id
        return False

    def default_type(self):
        return 'normal'

    def default_state(self):
        return 'draft'

    def default_display_balance(self):
        return 'credit-debit'

    def default_mandatory(self):
        return False

    def on_change_with_currency_digits(self, vals):
        currency_obj = Pool().get('currency.currency')
        if vals.get('currency'):
            currency = currency_obj.browse(vals['currency'])
            return currency.digits
        return 2

    def get_currency_digits(self, ids, name):
        res = {}
        for account in self.browse(ids):
            res[account.id] = account.currency.digits
        return res

    def get_balance(self, ids, name):
        res = {}
        line_obj = Pool().get('analytic_account.line')
        currency_obj = Pool().get('currency.currency')
        cursor = Transaction().cursor

        child_ids = self.search([('parent', 'child_of', ids)])
        all_ids = {}.fromkeys(ids + child_ids).keys()

        id2account = {}
        accounts = self.browse(all_ids)
        for account in accounts:
            id2account[account.id] = account

        line_query = line_obj.query_get()
        cursor.execute('SELECT a.id, ' \
                    'SUM((COALESCE(l.debit, 0) - COALESCE(l.credit, 0))), ' \
                    'c.currency ' \
                'FROM analytic_account_account a ' \
                    'LEFT JOIN analytic_account_line l ' \
                    'ON (a.id = l.account) ' \
                    'LEFT JOIN account_move_line ml ' \
                    'ON (ml.id = l.move_line) ' \
                    'LEFT JOIN account_account aa ' \
                    'ON (aa.id = ml.account) ' \
                    'LEFT JOIN company_company c ' \
                    'ON (c.id = aa.company) ' \
                'WHERE a.type != \'view\' ' \
                    'AND a.id IN (' + \
                        ','.join(('%s',) * len(all_ids)) + ') ' \
                    'AND ' + line_query + ' ' \
                    'AND a.active ' \
                'GROUP BY a.id, c.currency', all_ids)
        account_sum = {}
        id2currency = {}
        for account_id, sum, currency_id in cursor.fetchall():
            account_sum.setdefault(account_id, Decimal('0.0'))
            if currency_id != id2account[account_id].currency.id:
                currency = None
                if currency_id in id2currency:
                    currency = id2currency[currency_id]
                else:
                    currency = currency_obj.browse(currency_id)
                    id2currency[currency.id] = currency
                account_sum[account_id] += currency_obj.compute(currency, sum,
                        id2account[account_id].currency, round=True)
            else:
                account_sum[account_id] += currency_obj.round(
                        id2account[account_id].currency, sum)

        for account_id in ids:
            res.setdefault(account_id, Decimal('0.0'))
            child_ids = self.search([
                ('parent', 'child_of', [account_id]),
                ])
            to_currency = id2account[account_id].currency
            for child_id in child_ids:
                from_currency = id2account[child_id].currency
                res[account_id] += currency_obj.compute(from_currency,
                        account_sum.get(child_id, Decimal('0.0')), to_currency,
                        round=True)
            res[account_id] = currency_obj.round(to_currency, res[account_id])
            if id2account[account_id].display_balance == 'credit-debit':
                res[account_id] = - res[account_id]
        return res

    def get_credit_debit(self, ids, name):
        res = {}
        line_obj = Pool().get('analytic_account.line')
        currency_obj = Pool().get('currency.currency')
        cursor = Transaction().cursor

        if name not in ('credit', 'debit'):
            raise Exception('Bad argument')

        id2account = {}
        accounts = self.browse(ids)
        for account in accounts:
            res[account.id] = Decimal('0.0')
            id2account[account.id] = account

        line_query = line_obj.query_get()
        cursor.execute('SELECT a.id, ' \
                    'SUM(COALESCE(l.' + name + ', 0)), ' \
                    'c.currency ' \
                'FROM analytic_account_account a ' \
                    'LEFT JOIN analytic_account_line l ' \
                    'ON (a.id = l.account) ' \
                    'LEFT JOIN account_move_line ml ' \
                    'ON (ml.id = l.move_line) ' \
                    'LEFT JOIN account_account aa ' \
                    'ON (aa.id = ml.account) ' \
                    'LEFT JOIN company_company c ' \
                    'ON (c.id = aa.company) ' \
                'WHERE a.type != \'view\' ' \
                    'AND a.id IN (' + \
                        ','.join(('%s',) * len(ids)) + ') ' \
                    'AND ' + line_query + ' ' \
                    'AND a.active ' \
                'GROUP BY a.id, c.currency', ids)

        id2currency = {}
        for account_id, sum, currency_id in cursor.fetchall():
            if currency_id != id2account[account_id].currency.id:
                currency = None
                if currency_id in id2currency:
                    currency = id2currency[currency_id]
                else:
                    currency = currency_obj.browse(currency_id)
                    id2currency[currency.id] = currency
                res[account_id] += currency_obj.compute(currency, sum,
                        id2account[account_id].currency, round=True)
            else:
                res[account_id] += currency_obj.round(
                        id2account[account_id].currency, sum)
        return res

    def get_rec_name(self, ids, name):
        if not ids:
            return {}
        res = {}
        for account in self.browse(ids):
            if account.code:
                res[account.id] = account.code + ' - ' + unicode(account.name)
            else:
                res[account.id] = unicode(account.name)
        return res

    def search_rec_name(self, name, clause):
        ids = self.search([('code',) + clause[1:]], limit=1)
        if ids:
            return [('code',) + clause[1:]]
        else:
            return [(self._rec_name,) + clause[1:]]

    def convert_view(self, tree):
        res = tree.xpath('//field[@name=\'analytic_accounts\']')
        if not res:
            return
        element_accounts = res[0]

        root_account_ids = self.search([
            ('parent', '=', False),
            ])
        if not root_account_ids:
            element_accounts.getparent().getparent().remove(
                    element_accounts.getparent())
            return
        for account_id in root_account_ids:
            newelement = copy.copy(element_accounts)
            newelement.tag = 'label'
            newelement.set('name', 'analytic_account_' + str(account_id))
            element_accounts.addprevious(newelement)
            newelement = copy.copy(element_accounts)
            newelement.set('name', 'analytic_account_' + str(account_id))
            element_accounts.addprevious(newelement)
        parent = element_accounts.getparent()
        parent.remove(element_accounts)

    def analytic_accounts_fields_get(self, field, fields_names=None):
        res = {}
        if fields_names is None:
            fields_names = []

        root_account_ids = self.search([
            ('parent', '=', False),
            ])
        for account in self.browse(root_account_ids):
            name = 'analytic_account_' + str(account.id)
            if name in fields_names or not fields_names:
                res[name] = field.copy()
                res[name]['required'] = account.mandatory
                res[name]['string'] = account.name
                res[name]['relation'] = self._name
                res[name]['domain'] = PYSONEncoder().encode([
                    ('root', '=', account.id),
                    ('type', '=', 'normal')])
        return res

Account()


class OpenChartAccountInit(ModelView):
    'Open Chart Account Init'
    _name = 'analytic_account.account.open_chart_account.init'
    _description = __doc__
    start_date = fields.Date('Start Date')
    end_date = fields.Date('End Date')

OpenChartAccountInit()


class OpenChartAccount(Wizard):
    'Open Chart Of Account'
    _name = 'analytic_account.account.open_chart_account'
    states = {
        'init': {
            'result': {
                'type': 'form',
                'object': 'analytic_account.account.open_chart_account.init',
                'state': [
                    ('end', 'Cancel', 'tryton-cancel'),
                    ('open', 'Open', 'tryton-ok', True),
                ],
            },
        },
        'open': {
            'result': {
                'type': 'action',
                'action': '_action_open_chart',
                'state': 'end',
            },
        },
    }

    def _action_open_chart(self, data):
        model_data_obj = Pool().get('ir.model.data')
        act_window_obj = Pool().get('ir.action.act_window')
        act_window_id = model_data_obj.get_id('analytic_account',
                'act_account_tree2')
        res = act_window_obj.read(act_window_id)
        res['pyson_context'] = PYSONEncoder().encode({
            'start_date': data['form']['start_date'],
            'end_date': data['form']['end_date'],
            })
        return res

OpenChartAccount()


class AccountSelection(ModelSQL, ModelView):
    'Analytic Account Selection'
    _name = 'analytic_account.account.selection'
    _description = __doc__
    _rec_name = 'id'

    accounts = fields.Many2Many(
            'analytic_account.account-analytic_account.account.selection',
            'selection', 'account', 'Accounts')

    def __init__(self):
        super(AccountSelection, self).__init__()
        self._constraints += [
            ('check_root', 'root_account'),
        ]
        self._error_messages.update({
            'root_account': 'Can not have many accounts with the same root ' \
                    'or a missing mandatory root account!',
        })

    def check_root(self, ids):
        "Check Root"
        account_obj = Pool().get('analytic_account.account')

        root_account_ids = account_obj.search([
            ('parent', '=', False),
            ])
        root_accounts = account_obj.browse(root_account_ids)

        selections = self.browse(ids)
        for selection in selections:
            roots = []
            for account in selection.accounts:
                if account.root.id in roots:
                    return False
                roots.append(account.root.id)
            if Transaction().user: #Root can by pass
                for account in root_accounts:
                    if account.mandatory:
                        if not account.id in roots:
                            return False
        return True

AccountSelection()


class AccountAccountSelection(ModelSQL):
    'Analytic Account - Analytic Account Selection'
    _name = 'analytic_account.account-analytic_account.account.selection'
    _description = __doc__
    _table = 'analytic_account_account_selection_rel'
    selection = fields.Many2One('analytic_account.account.selection',
            'Selection', ondelete='CASCADE', required=True, select=1)
    account = fields.Many2One('analytic_account.account', 'Account',
            ondelete='RESTRICT', required=True, select=1)

AccountAccountSelection()
