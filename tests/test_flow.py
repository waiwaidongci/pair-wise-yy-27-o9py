import os, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from database import CollationDB, DomainError

class CollationFlowTest(unittest.TestCase):
    def setUp(self):
        fd,self.path=tempfile.mkstemp(suffix=".db"); os.close(fd); self.db=CollationDB(self.path)
        self.owner=self.db.add_user("负责人","owner"); self.editor=self.db.add_user("编辑","editor"); self.reviewer=self.db.add_user("审阅","reviewer"); self.outsider=self.db.add_user("外部","reviewer")
        self.work=self.db.create_work("残卷","异文比较",self.owner)
        self.w1=self.db.add_witness(self.work,"甲本","version"); self.w2=self.db.add_witness(self.work,"乙本","fragment","馆藏残片","中段缺页")
        self.db.grant_witness_editor(self.w2,self.editor,self.owner); self.db.grant_work_access(self.work,self.reviewer,"view",self.owner)
        self.passage=self.db.add_passage(self.work,"第一节","春水东流，故人南去。",self.owner)
        self.db.align_passage(self.passage,self.w1,"春水东流，故人南去。",1,self.owner)
        self.db.align_passage(self.passage,self.w2,"春水东流，[缺页]",2,self.editor)
    def tearDown(self): self.db.close(); os.unlink(self.path)

    def test_multilayer_revision_snapshot_export_and_lock(self):
        variant=self.db.create_variant(self.passage,self.w2,"春水东流，故人南去。","按语义补足",self.editor,0)
        rev=self.db.update_variant(variant,"春水东流，[不可辨]人南去。","墨迹受损，不再直接补写",self.editor,1)
        self.assertEqual(2,rev)
        snap=self.db.get_snapshot(self.passage,self.w2,2,self.owner)
        self.assertEqual(2,snap["layer"])
        exported=self.db.export_collation(self.work,self.reviewer)
        self.assertEqual(1,exported["gap_count"])
        self.assertTrue(exported["passages"][0]["variants"][0]["notes"] == [])
        self.db.lock_passage(self.passage,self.owner,"定稿")
        with self.assertRaisesRegex(DomainError,"锁定"):
            self.db.update_variant(variant,"另一文本","无意义修改",self.editor,2)

    def test_optimistic_lock_permission_and_mark_validation(self):
        first=self.db.create_variant(self.passage,self.w2,"补足一","理由一",self.editor,0)
        with self.assertRaisesRegex(DomainError,"版本冲突"):
            self.db.create_variant(self.passage,self.w2,"补足二","理由二",self.editor,0)
        with self.assertRaisesRegex(DomainError,"无权"):
            self.db.create_variant(self.passage,self.w2,"补足三","理由三",self.reviewer,1)
        with self.assertRaisesRegex(DomainError,"无权"):
            self.db.export_collation(self.work,self.outsider)
        with self.assertRaisesRegex(DomainError,"括号"):
            self.db.align_passage(self.passage,self.w1,"文本[未闭合",9,self.owner)

    def test_two_versions_save_simultaneously_without_blocking_each_other(self):
        # 两位编辑分别在甲本、乙本上同时基于 0 提交，各自成功。
        v1=self.db.create_variant(self.passage,self.w1,"甲本补足","甲本理由",self.owner,0)
        v2=self.db.create_variant(self.passage,self.w2,"乙本补足","乙本理由",self.editor,0)
        self.assertEqual(1,self.db.conn.execute("SELECT layer FROM variants WHERE id=?",(v1,)).fetchone()["layer"])
        self.assertEqual(1,self.db.conn.execute("SELECT layer FROM variants WHERE id=?",(v2,)).fetchone()["layer"])
        # 同一版本再次提交必须带当前层号；过期的只拦这一版，不影响另一版。
        n1=self.db.update_variant(v1,"甲本新层","甲本新理由",self.owner,1)
        self.assertEqual(2,n1)
        with self.assertRaisesRegex(DomainError,"版本冲突"):
            self.db.update_variant(v2,"乙本新层","乙本新理由",self.editor,0)
        # 甲本继续推进不受乙本过期影响。
        n1b=self.db.update_variant(v1,"甲本再新层","甲本再新理由",self.owner,2)
        self.assertEqual(3,n1b)
        # 乙本带上当前层号 1 仍可成功。
        n2=self.db.update_variant(v2,"乙本新层","乙本新理由",self.editor,1)
        self.assertEqual(2,n2)

    def test_merge_confirms_then_stales_on_selected_version_change(self):
        v1=self.db.create_variant(self.passage,self.w1,"甲本补足","甲本理由",self.owner,0)
        v2=self.db.create_variant(self.passage,self.w2,"乙本补足","乙本理由",self.editor,0)
        # 负责人选定两版最新校记生成合并稿。
        merge_id=self.db.create_merge(self.work,self.passage,self.owner,"合并写定文本",[{"witness_id":self.w1},{"witness_id":self.w2}])
        self.assertGreater(merge_id,0)
        exported=self.db.export_collation(self.work,self.reviewer)
        self.assertEqual(1,len(exported["merged_drafts"]))
        self.assertEqual("合并写定文本",exported["merged_drafts"][0]["merged_text"])
        self.assertEqual([],exported["conflicts"])
        self.assertEqual([],exported["undecided_passages"])
        # 被选版本（乙本）又改过，原合并稿失效，需重新确认。
        self.db.update_variant(v2,"乙本再改","乙本再改理由",self.editor,1)
        exported=self.db.export_collation(self.work,self.reviewer)
        self.assertEqual([],exported["merged_drafts"])
        self.assertEqual(1,len(exported["conflicts"]))
        self.assertEqual(1,len(exported["undecided_passages"]))
        self.assertEqual({self.w1,self.w2},{c["witness_id"] for c in exported["conflicts"][0]["witnesses"]})
        # 重新确认后恢复合并稿导出。
        self.db.create_merge(self.work,self.passage,self.owner,"重新写定",[{"witness_id":self.w1},{"witness_id":self.w2}])
        exported=self.db.export_collation(self.work,self.reviewer)
        self.assertEqual(1,len(exported["merged_drafts"]))
        self.assertEqual("重新写定",exported["merged_drafts"][0]["merged_text"])
        self.assertEqual([],exported["undecided_passages"])

    def test_merge_requires_owner_and_variant(self):
        self.db.create_variant(self.passage,self.w2,"乙本补足","乙本理由",self.editor,0)
        with self.assertRaisesRegex(DomainError,"负责人"):
            self.db.create_merge(self.work,self.passage,self.editor,"合并",[{"witness_id":self.w2}])
        with self.assertRaisesRegex(DomainError,"校记"):
            self.db.create_merge(self.work,self.passage,self.owner,"合并",[{"witness_id":self.w1},{"witness_id":self.w2}])

if __name__=="__main__": unittest.main()
