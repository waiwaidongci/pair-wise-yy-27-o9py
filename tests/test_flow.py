import os, sys, tempfile, threading, unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from database import CollationDB, DomainError

class CollationFlowTest(unittest.TestCase):
    def setUp(self):
        fd,self.path=tempfile.mkstemp(suffix=".db"); os.close(fd); self.db=CollationDB(self.path)
        self.owner=self.db.add_user("负责人","owner"); self.editor=self.db.add_user("编辑","editor"); self.editor2=self.db.add_user("编辑乙","editor"); self.reviewer=self.db.add_user("审阅","reviewer"); self.outsider=self.db.add_user("外部","reviewer")
        self.work=self.db.create_work("残卷","异文比较",self.owner)
        self.w1=self.db.add_witness(self.work,"甲本","version"); self.w2=self.db.add_witness(self.work,"乙本","fragment","馆藏残片","中段缺页")
        self.db.grant_witness_editor(self.w1,self.editor,self.owner); self.db.grant_witness_editor(self.w2,self.editor2,self.owner)
        self.db.grant_work_access(self.work,self.reviewer,"view",self.owner)
        self.passage=self.db.add_passage(self.work,"第一节","春水东流，故人南去。",self.owner)
        self.db.align_passage(self.passage,self.w1,"春水东流，故人南去。",1,self.owner)
        self.db.align_passage(self.passage,self.w2,"春水东流，[缺页]",2,self.editor2)
    def tearDown(self): self.db.close(); os.unlink(self.path)

    def test_layers_advance_per_witness_and_save_together(self):
        # 甲本、乙本各自基于层 0 同时推进，互不拦截
        va=self.db.create_variant(self.passage,self.w1,"甲本改文","甲本理由充分",self.editor,0)
        vb=self.db.create_variant(self.passage,self.w2,"乙本改文","乙本理由充分",self.editor2,0)
        self.assertEqual(1,self.db._current_layer(self.passage,self.w1))
        self.assertEqual(1,self.db._current_layer(self.passage,self.w2))
        # 同一版本再次提交必须携带当前层号
        with self.assertRaisesRegex(DomainError,"版本冲突"):
            self.db.update_variant(va,"甲本旧层提交","仍拿着旧层号",self.editor,0)
        # 甲本进到层 2，不影响乙本继续基于层 1 提交
        self.db.update_variant(va,"甲本新层","甲本继续修订",self.editor,1)
        self.db.update_variant(vb,"乙本新层","乙本继续修订",self.editor2,1)
        self.assertEqual(2,self.db._current_layer(self.passage,self.w1))
        self.assertEqual(2,self.db._current_layer(self.passage,self.w2))
        snap=self.db.get_snapshot(self.passage,4,self.owner)
        self.assertEqual(2,snap["layer"])

    def test_two_witnesses_submit_concurrently_both_succeed(self):
        errors=[]
        def create(wid,uid):
            try: self.db.create_variant(self.passage,wid,f"改文{wid}","理由足够说明",uid,0)
            except Exception as exc: errors.append(exc)
        t1=threading.Thread(target=create,args=(self.w1,self.editor))
        t2=threading.Thread(target=create,args=(self.w2,self.editor2))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual([],errors)
        self.assertEqual(1,self.db._current_layer(self.passage,self.w1))
        self.assertEqual(1,self.db._current_layer(self.passage,self.w2))

    def test_merge_confirmed_exported_and_invalidated_by_new_layer(self):
        va=self.db.create_variant(self.passage,self.w1,"甲本定稿文","甲本取舍依据",self.editor,0)
        vb=self.db.create_variant(self.passage,self.w2,"乙本定稿文","乙本取舍依据",self.editor2,0)
        self.db.confirm_merge(self.passage,[self.w1,self.w2],"甲乙合并定稿",self.owner)
        exported=self.db.export_collation(self.work,self.reviewer)
        self.assertEqual(1,len(exported["merged"]))
        self.assertEqual("甲乙合并定稿",exported["merged"][0]["merged_text"])
        self.assertEqual([],exported["conflicts"]); self.assertEqual([],exported["undecided"])
        self.assertEqual(1,exported["gap_count"])
        # 被选版本（乙本）再改：原合并稿失效，旧合并文本不得继续导出
        self.db.update_variant(vb,"乙本又改了","发现新的墨迹",self.editor2,1)
        exported=self.db.export_collation(self.work,self.reviewer)
        self.assertEqual([],exported["merged"])
        self.assertEqual(1,len(exported["conflicts"]))
        entry=exported["conflicts"][0]
        self.assertEqual("stale",entry["merge_status"])
        self.assertNotIn("merged_text",entry)
        self.assertEqual([self.w2],entry["changed_witness_ids"])
        self.assertIn("乙本",entry["stale_reason"])
        # 甲本（未被改动的被选版本）不是过期源，但甲本的旧校记也不能再随合并稿导出
        self.assertEqual({self.w1,self.w2},{c["witness_id"] for c in entry["candidates"]})
        self.assertEqual("乙本又改了",next(c for c in entry["candidates"] if c["witness_id"]==self.w2)["proposed_text"])
        # 重新确认后恢复合并稿导出
        self.db.confirm_merge(self.passage,[self.w1,self.w2],"重新合并定稿",self.owner)
        exported=self.db.export_collation(self.work,self.reviewer)
        self.assertEqual("重新合并定稿",exported["merged"][0]["merged_text"])
        self.assertEqual([],exported["conflicts"])

    def test_new_unselected_witness_breaks_confirmation(self):
        self.db.create_variant(self.passage,self.w1,"甲本改文","甲本取舍依据",self.editor,0)
        self.db.confirm_merge(self.passage,[self.w1],"仅依甲本定稿",self.owner)
        self.db.create_variant(self.passage,self.w2,"乙本新校记","乙本后来补入",self.editor2,0)
        exported=self.db.export_collation(self.work,self.reviewer)
        self.assertEqual([],exported["merged"])
        self.assertEqual(1,len(exported["conflicts"]))
        self.assertIn("乙本",exported["conflicts"][0]["stale_reason"])

    def test_undecided_passages_listed_separately(self):
        self.db.create_variant(self.passage,self.w1,"甲本改文","甲本取舍依据",self.editor,0)
        self.db.create_variant(self.passage,self.w2,"乙本改文","乙本取舍依据",self.editor2,0)
        exported=self.db.export_collation(self.work,self.reviewer)
        self.assertEqual([],exported["merged"]); self.assertEqual([],exported["conflicts"])
        self.assertEqual(1,len(exported["undecided"]))
        cand={c["witness_id"]:c for c in exported["undecided"][0]["candidates"]}
        self.assertEqual({self.w1,self.w2},set(cand))
        self.assertEqual(1,cand[self.w1]["layer"])

    def test_merge_requires_owner_and_existing_candidate(self):
        self.db.create_variant(self.passage,self.w1,"甲本改文","甲本取舍依据",self.editor,0)
        with self.assertRaisesRegex(DomainError,"负责人"):
            self.db.confirm_merge(self.passage,[self.w1],"越权定稿",self.editor)
        with self.assertRaisesRegex(DomainError,"已有校记"):
            self.db.confirm_merge(self.passage,[self.w2],"选了没有校记的版本",self.owner)
        with self.assertRaisesRegex(DomainError,"至少选定"):
            self.db.confirm_merge(self.passage,[],"未选版本",self.owner)

    def test_stale_layer_permission_lock_and_mark_validation(self):
        first=self.db.create_variant(self.passage,self.w2,"补足一","理由一",self.editor2,0)
        with self.assertRaisesRegex(DomainError,"版本冲突"):
            self.db.create_variant(self.passage,self.w2,"补足二","理由二",self.editor2,0)
        with self.assertRaisesRegex(DomainError,"无权"):
            self.db.create_variant(self.passage,self.w1,"补足三","理由三",self.reviewer,0)
        with self.assertRaisesRegex(DomainError,"无权"):
            self.db.export_collation(self.work,self.outsider)
        with self.assertRaisesRegex(DomainError,"括号"):
            self.db.align_passage(self.passage,self.w1,"文本[未闭合",9,self.owner)
        # 锁定段落：原有权限照旧，只是任何新修订都被拒绝
        self.db.lock_passage(self.passage,self.owner,"定稿")
        with self.assertRaisesRegex(DomainError,"锁定"):
            self.db.update_variant(first,"另一文本","无意义修改",self.editor2,1)
        exported=self.db.export_collation(self.work,self.reviewer)
        self.assertTrue(exported["undecided"][0]["locked"])

if __name__=="__main__": unittest.main()
