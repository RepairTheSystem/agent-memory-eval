import unittest

from memeval.llm import ScriptedLLM
from memeval.pipeline import Fixes
from memeval.profile import ProfileSystem


class ProfileInvariants(unittest.TestCase):
    def setUp(self):
        self.memory = ProfileSystem(ScriptedLLM(), Fixes.search())
        self.user = 'synthetic-user'
        self.org = self.memory._current_org(self.user)

    def apply(self, op, message, kind='session'):
        return self.memory.apply_ops(self.user,[op],kind=kind,
            msgs=[{'role':'user','text':message}],org=self.org)

    def add_preference(self):
        text = 'Предпочитаю короткие ответы'
        accepted = self.apply({'op':'add','section':'prefs','text':text,'evidence':text},text)
        self.assertEqual(len(accepted),1)
        return dict(self.memory._prof(self.user)['lines'][0])

    def test_supported_fact_is_saved_with_audit_entry(self):
        self.add_preference()
        self.assertEqual(self.memory._prof(self.user)['version'],1)
        self.assertEqual(self.memory.journal[-1]['verdict'],'принята')

    def test_unsupported_evidence_cannot_create_fact(self):
        accepted = self.apply({'op':'add','section':'prefs','text':'Нужны таблицы','evidence':'Нужны таблицы'},'Привет')
        self.assertEqual(accepted,[])
        self.assertEqual(self.memory._prof(self.user)['lines'],[])
        self.assertEqual(self.memory.journal[-1]['verdict'],'отклонена')

    def test_stale_old_value_cannot_overwrite_newer_fact(self):
        line = self.add_preference()
        new = 'Предпочитаю подробные ответы'
        accepted = self.apply({'op':'replace','id':line['id'],'old':'Несуществующее старое значение',
                               'text':new,'evidence':new},new)
        self.assertEqual(accepted,[])
        self.assertEqual(self.memory._prof(self.user)['lines'][0]['text'],line['text'])

    def test_replacement_retains_previous_value(self):
        line = self.add_preference()
        new = 'Предпочитаю подробные ответы'
        accepted = self.apply({'op':'replace','id':line['id'],'old':line['text'],'text':new,'evidence':new},new)
        self.assertEqual(len(accepted),1)
        saved = self.memory._prof(self.user)['lines'][0]
        self.assertEqual(saved['text'],new)
        self.assertEqual(saved['was'],line['text'])

    def test_duplicate_fact_does_not_create_second_line(self):
        line = self.add_preference()
        self.apply({'op':'add','section':'prefs','text':line['text'],'evidence':line['text']},line['text'])
        self.assertEqual(len(self.memory._prof(self.user)['lines']),1)

    def test_sleep_cannot_remove_numeric_information(self):
        line = self.memory._new_line(self.user,'prefs','Бюджет 12000 рублей',self.memory.clock.t)
        accepted = self.apply({'op':'replace','id':line['id'],'text':'Есть бюджет'},'',kind='sleep')
        self.assertEqual(accepted,[])
        self.assertEqual(self.memory._prof(self.user)['lines'][0]['text'],'Бюджет 12000 рублей')

    def test_archive_remains_retrievable(self):
        line = self.add_preference()
        self.apply({'op':'archive','id':line['id'],'reason':'test'},'',kind='sleep')
        self.assertEqual(len(self.memory._prof(self.user)['lines']),0)
        found = self.memory.search(self.user,'короткие ответы')
        self.assertIn('короткие ответы',str(found))

    def test_profiles_are_isolated_between_users(self):
        self.add_preference()
        self.assertEqual(self.memory._prof('another-user')['lines'],[])

    def test_temporary_fact_requires_expiration(self):
        message = 'Готовлю отчёт сегодня'
        accepted = self.apply({'op':'add','section':'current','text':message,'evidence':message},message)
        self.assertEqual(accepted,[])

    def test_provider_free_mock_suite(self):
        from memeval.mock_scenarios import run_all
        rows = run_all()
        self.assertEqual(len(rows),12)
        self.assertTrue(all(r['after']['passed'] for r in rows))


if __name__ == '__main__':
    unittest.main()
