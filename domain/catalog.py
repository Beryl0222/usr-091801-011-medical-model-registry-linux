"""腹部 CT 模型的适用范围目录。

每个条目是一种可判定的腹部病症（condition），字段包括：
- code: 稳定标识，验证指标与禁用场景均引用 code；
- name: 中文名；
- organ: 适用器官/解剖区域（取值见 ORGANS）；
- emergency: 是否属于急诊高代价漏报病种（急诊亚组门槛单独核算）；
- protocols: 支持的扫描协议（取值见 PROTOCOLS）。

目录用于把“专家级”宣传收敛到具体病种×协议：发布版本只能声明覆盖目录中
存在的条目，提交的协议不在条目支持范围内将被直接拒绝。
"""

# 扫描协议
NONCONTRAST = "noncontrast"      # 平扫
ARTERIAL = "arterial"            # 动脉期
PORTAL = "portal_venous"         # 门脉期
DELAYED = "delayed"              # 延迟期
CTA = "cta"                      # CT 血管造影
UROGRAPHY = "ct_urology"         #  CT 尿路造影（排泄期）
ENTEROGRAPHY = "ct_enterography"  # 小肠造影

PROTOCOLS = frozenset(
    {NONCONTRAST, ARTERIAL, PORTAL, DELAYED, CTA, UROGRAPHY, ENTEROGRAPHY}
)

# 器官 / 解剖区域
LIVER = "liver"
BILIARY = "biliary"
PANCREAS = "pancreas"
SPLEEN = "spleen"
GI = "gi_tract"
PERITONEUM = "peritoneum"
RETROPERITONEUM = "retroperitoneum"
KIDNEY = "kidney_ureter"
BLADDER = "bladder"
ADRENAL = "adrenal"
VASCULAR = "vasculature"
ABDOMINAL_WALL = "abdominal_wall"
GYN = "gynecologic"
PROSTATE = "prostate"
TRAUMA = "generic_trauma"

ORGANS = frozenset(
    {
        LIVER, BILIARY, PANCREAS, SPLEEN, GI, PERITONEUM, RETROPERITONEUM,
        KIDNEY, BLADDER, ADRENAL, VASCULAR, ABDOMINAL_WALL, GYN, PROSTATE, TRAUMA,
    }
)

# (code, 中文名, 器官, 急诊, 支持协议)
_ENTRIES = [
    # ── 肝脏 ──────────────────────────────────────────────
    ("liver_mass", "肝占位性病变", LIVER, False, (ARTERIAL, PORTAL, DELAYED)),
    ("hcc", "肝细胞癌", LIVER, False, (ARTERIAL, PORTAL, DELAYED)),
    ("liver_metastasis", "肝转移瘤", LIVER, False, (PORTAL, ARTERIAL, DELAYED)),
    ("cholangiocarcinoma", "肝内胆管细胞癌", LIVER, False, (PORTAL, DELAYED)),
    ("hepatic_adenoma", "肝腺瘤", LIVER, False, (ARTERIAL, PORTAL)),
    ("hemangioma", "肝血管瘤", LIVER, False, (ARTERIAL, PORTAL, DELAYED)),
    ("focal_fatty_liver", "局灶性脂肪肝", LIVER, False, (NONCONTRAST, PORTAL)),
    ("hepatic_steatosis", "弥漫性脂肪肝", LIVER, False, (NONCONTRAST, PORTAL)),
    ("liver_cyst", "肝囊肿", LIVER, False, (NONCONTRAST, PORTAL, DELAYED)),
    ("calcified_liver_lesion", "肝钙化灶", LIVER, False, (NONCONTRAST, PORTAL)),
    ("cirrhosis", "肝硬化", LIVER, False, (PORTAL, DELAYED)),
    ("portal_hypertension", "门脉高压", LIVER, False, (PORTAL, CTA)),
    ("hepatomegaly", "肝大", LIVER, False, (NONCONTRAST, PORTAL)),
    ("liver_laceration", "肝裂伤", LIVER, True, (PORTAL, ARTERIAL)),
    ("liver_abscess", "肝脓肿", LIVER, True, (PORTAL, DELAYED)),
    ("liver_infarct", "肝梗死", LIVER, True, (ARTERIAL, PORTAL)),
    ("portal_vein_thrombosis", "门静脉血栓形成", LIVER, True, (PORTAL, CTA)),
    ("budd_chiari", "布加综合征", LIVER, True, (CTA, PORTAL)),
    ("biloma", "胆汁瘤", LIVER, True, (PORTAL, DELAYED)),
    ("biliary_dilatation", "肝内胆管扩张", BILIARY, False, (PORTAL, NONCONTRAST)),
    # ── 胆道 / 胆囊 ───────────────────────────────────────
    ("gallstone", "胆囊结石", BILIARY, False, (NONCONTRAST, PORTAL)),
    ("gallbladder_polyp", "胆囊息肉", BILIARY, False, (PORTAL,)),
    ("gallbladder_carcinoma", "胆囊癌", BILIARY, False, (PORTAL, DELAYED)),
    ("gallbladder_wall_thickening", "胆囊壁增厚", BILIARY, False, (PORTAL,)),
    ("chronic_cholecystitis", "慢性胆囊炎", BILIARY, False, (PORTAL, NONCONTRAST)),
    ("acute_cholecystitis", "急性胆囊炎", BILIARY, True, (PORTAL, NONCONTRAST)),
    ("choledocholithiasis", "胆总管结石", BILIARY, True, (NONCONTRAST, PORTAL)),
    ("acute_cholangitis", "急性胆管炎", BILIARY, True, (PORTAL, NONCONTRAST)),
    ("biliary_leak", "胆漏", BILIARY, True, (PORTAL, DELAYED)),
    ("pneumobilia", "胆道积气", BILIARY, False, (NONCONTRAST, PORTAL)),
    # ── 胰腺 ──────────────────────────────────────────────
    ("pancreatic_mass", "胰腺占位性病变", PANCREAS, False, (PORTAL, DELAYED, ARTERIAL)),
    ("pancreatic_cystic_lesion", "胰腺囊性病变", PANCREAS, False, (PORTAL, DELAYED)),
    ("pancreatic_calcification", "胰腺钙化", PANCREAS, False, (NONCONTRAST, PORTAL)),
    ("pancreatic_atrophy", "胰腺萎缩", PANCREAS, False, (PORTAL, NONCONTRAST)),
    ("chronic_pancreatitis", "慢性胰腺炎", PANCREAS, False, (PORTAL, NONCONTRAST)),
    ("acute_pancreatitis", "急性胰腺炎", PANCREAS, True, (PORTAL, NONCONTRAST)),
    ("necrotizing_pancreatitis", "坏死性胰腺炎", PANCREAS, True, (PORTAL, ARTERIAL, DELAYED)),
    ("peripancreatic_fluid", "胰周积液", PANCREAS, True, (PORTAL, DELAYED)),
    # ── 脾脏 ──────────────────────────────────────────────
    ("splenomegaly", "脾大", SPLEEN, False, (NONCONTRAST, PORTAL)),
    ("splenic_cyst", "脾囊肿", SPLEEN, False, (NONCONTRAST, PORTAL)),
    ("splenic_laceration", "脾裂伤", SPLEEN, True, (PORTAL, ARTERIAL)),
    ("splenic_infarct", "脾梗死", SPLEEN, False, (ARTERIAL, PORTAL)),
    ("splenic_abscess", "脾脓肿", SPLEEN, True, (PORTAL, DELAYED)),
    ("splenic_artery_aneurysm", "脾动脉瘤", SPLEEN, True, (CTA, ARTERIAL)),
    # ── 胃肠道 / 阑尾 ─────────────────────────────────────
    ("gastric_wall_thickening", "胃壁增厚", GI, False, (PORTAL,)),
    ("perforated_gastric_ulcer", "胃溃疡穿孔", GI, True, (NONCONTRAST, PORTAL)),
    ("small_bowel_mass", "小肠肿瘤", GI, False, (ENTEROGRAPHY, PORTAL)),
    ("gist", "胃肠间质瘤", GI, False, (PORTAL, ARTERIAL)),
    ("colon_mass", "结肠占位性病变", GI, False, (PORTAL,)),
    ("colorectal_cancer", "结直肠癌", GI, False, (PORTAL,)),
    ("colonic_polyp", "结肠息肉", GI, False, (ENTEROGRAPHY, PORTAL)),
    ("diverticulosis", "结肠憩室病", GI, False, (NONCONTRAST, PORTAL)),
    ("diverticulitis", "急性憩室炎", GI, True, (PORTAL, NONCONTRAST)),
    ("colitis", "结肠炎", GI, False, (PORTAL, NONCONTRAST)),
    ("inflammatory_bowel_disease", "炎症性肠病", GI, False, (ENTEROGRAPHY, PORTAL)),
    ("crohn_active", "活动期克罗恩病", GI, False, (ENTEROGRAPHY, PORTAL)),
    ("ulcerative_colitis", "溃疡性结肠炎", GI, False, (ENTEROGRAPHY, PORTAL)),
    ("bowel_obstruction", "机械性肠梗阻", GI, True, (NONCONTRAST, PORTAL)),
    ("adynamic_ileus", "麻痹性肠梗阻", GI, False, (NONCONTRAST, PORTAL)),
    ("postoperative_ileus", "术后肠梗阻", GI, False, (NONCONTRAST, PORTAL)),
    ("intussusception", "肠套叠", GI, True, (NONCONTRAST, PORTAL)),
    ("sigmoid_volvulus", "乙状结肠扭转", GI, True, (NONCONTRAST, PORTAL)),
    ("cecal_volvulus", "盲肠扭转", GI, True, (NONCONTRAST, PORTAL)),
    ("bowel_ischemia", "肠缺血", GI, True, (CTA, PORTAL, NONCONTRAST)),
    ("pneumatosis_intestinalis", "肠壁积气", GI, True, (NONCONTRAST, PORTAL)),
    ("portal_venous_gas", "门静脉积气", GI, True, (NONCONTRAST, PORTAL)),
    ("toxic_megacolon", "中毒性巨结肠", GI, True, (NONCONTRAST, PORTAL)),
    ("fecal_impaction", "粪嵌塞", GI, False, (NONCONTRAST,)),
    ("anastomotic_leak", "消化道吻合口漏", GI, True, (NONCONTRAST, PORTAL, DELAYED)),
    ("appendicitis", "急性阑尾炎", GI, True, (NONCONTRAST, PORTAL)),
    ("appendicolith", "阑尾石", GI, False, (NONCONTRAST, PORTAL)),
    ("appendiceal_mucocele", "阑尾黏液囊肿", GI, False, (PORTAL, NONCONTRAST)),
    ("epiploic_appendagitis", "网膜附件炎", GI, True, (PORTAL, NONCONTRAST)),
    ("omental_infarction", "网膜梗死", GI, True, (PORTAL, NONCONTRAST)),
    ("mesenteric_adenitis", "肠系膜淋巴结炎", GI, False, (PORTAL, NONCONTRAST)),
    ("incarcerated_hernia", "嵌顿性疝", GI, True, (NONCONTRAST, PORTAL)),
    ("abdominal_wall_hernia", "腹壁疝", GI, False, (NONCONTRAST, PORTAL)),
    # ── 腹膜 / 腹膜后 ─────────────────────────────────────
    ("ascites", "腹腔积液", PERITONEUM, False, (NONCONTRAST, PORTAL)),
    ("free_intraperitoneal_fluid", "腹腔游离积液", PERITONEUM, False, (NONCONTRAST, PORTAL)),
    ("hemoperitoneum", "腹腔积血", PERITONEUM, True, (NONCONTRAST, PORTAL, ARTERIAL)),
    ("pneumoperitoneum", "气腹", PERITONEUM, True, (NONCONTRAST, PORTAL)),
    ("viscus_perforation", "空腔脏器穿孔", PERITONEUM, True, (NONCONTRAST, PORTAL)),
    ("intraabdominal_abscess", "腹腔脓肿", PERITONEUM, True, (PORTAL, DELAYED)),
    ("peritoneal_carcinomatosis", "腹膜癌病", PERITONEUM, False, (PORTAL, DELAYED)),
    ("retroperitoneal_mass", "腹膜后肿物", RETROPERITONEUM, False, (PORTAL, DELAYED)),
    ("retroperitoneal_fibrosis", "腹膜后纤维化", RETROPERITONEUM, False, (PORTAL, DELAYED, UROGRAPHY)),
    ("abdominal_lymphadenopathy", "腹腔淋巴结肿大", RETROPERITONEUM, False, (PORTAL,)),
    # ── 肾 / 输尿管 / 膀胱 / 肾上腺 ─────────────────────────
    ("renal_mass", "肾占位性病变", KIDNEY, False, (PORTAL, ARTERIAL, DELAYED)),
    ("renal_cell_carcinoma", "肾细胞癌", KIDNEY, False, (ARTERIAL, PORTAL, DELAYED)),
    ("renal_cyst", "肾囊肿", KIDNEY, False, (NONCONTRAST, PORTAL, DELAYED)),
    ("nephrolithiasis", "肾结石", KIDNEY, False, (NONCONTRAST, UROGRAPHY)),
    ("hydronephrosis", "肾积水", KIDNEY, False, (NONCONTRAST, UROGRAPHY, PORTAL)),
    ("ureteral_stone", "输尿管结石", KIDNEY, True, (NONCONTRAST, UROGRAPHY)),
    ("acute_pyelonephritis", "急性肾盂肾炎", KIDNEY, True, (PORTAL, NONCONTRAST)),
    ("perinephric_stranding", "肾周脂肪囊渗出", KIDNEY, False, (NONCONTRAST, PORTAL)),
    ("renal_laceration", "肾裂伤", KIDNEY, True, (PORTAL, ARTERIAL)),
    ("renal_infarct", "肾梗死", KIDNEY, True, (ARTERIAL, CTA)),
    ("urinoma", "尿性囊肿", KIDNEY, True, (PORTAL, DELAYED, UROGRAPHY)),
    ("adrenal_mass", "肾上腺占位", ADRENAL, False, (NONCONTRAST, PORTAL, DELAYED)),
    ("adrenal_adenoma", "肾上腺腺瘤", ADRENAL, False, (NONCONTRAST, PORTAL, DELAYED)),
    ("adrenal_hemorrhage", "肾上腺出血", ADRENAL, True, (NONCONTRAST, PORTAL)),
    ("bladder_wall_thickening", "膀胱壁增厚", BLADDER, False, (UROGRAPHY, PORTAL)),
    ("bladder_mass", "膀胱占位", BLADDER, False, (UROGRAPHY, PORTAL, DELAYED)),
    ("bladder_stone", "膀胱结石", BLADDER, False, (NONCONTRAST, UROGRAPHY)),
    # ── 血管 ──────────────────────────────────────────────
    ("abdominal_aortic_aneurysm", "腹主动脉瘤", VASCULAR, False, (CTA, NONCONTRAST, ARTERIAL)),
    ("ruptured_aaa", "腹主动脉瘤破裂", VASCULAR, True, (CTA, NONCONTRAST, ARTERIAL)),
    ("abdominal_aortic_dissection", "腹主动脉夹层", VASCULAR, True, (CTA,)),
    ("active_arterial_extravasation", "活动性造影剂外渗", VASCULAR, True, (CTA, ARTERIAL)),
    ("acute_mesenteric_ischemia", "急性肠系膜缺血", VASCULAR, True, (CTA,)),
    ("smv_thrombosis", "肠系膜上静脉血栓形成", VASCULAR, True, (CTA, PORTAL)),
    ("renal_artery_stenosis", "肾动脉狭窄", VASCULAR, False, (CTA,)),
    ("iliac_artery_aneurysm", "髂动脉瘤", VASCULAR, False, (CTA, NONCONTRAST)),
    ("ivc_thrombosis", "下腔静脉血栓形成", VASCULAR, True, (CTA, PORTAL)),
    ("portosystemic_collaterals", "门体侧支循环开放", VASCULAR, False, (PORTAL, CTA)),
    # ── 腹壁 / 妇科 / 前列腺 / 通用创伤 ─────────────────────
    ("abdominal_wall_abscess", "腹壁脓肿", ABDOMINAL_WALL, True, (PORTAL, NONCONTRAST)),
    ("rectus_sheath_hematoma", "腹直肌鞘血肿", ABDOMINAL_WALL, True, (NONCONTRAST, PORTAL)),
    ("desmoid_tumor", "腹壁硬纤维瘤", ABDOMINAL_WALL, False, (PORTAL,)),
    ("ovarian_mass", "卵巢占位", GYN, False, (PORTAL, DELAYED)),
    ("ovarian_torsion", "卵巢扭转", GYN, True, (PORTAL, CTA)),
    ("ectopic_pregnancy", "异位妊娠", GYN, True, (PORTAL, NONCONTRAST)),
    ("benign_prostatic_enlargement", "良性前列腺增生", PROSTATE, False, (UROGRAPHY, NONCONTRAST)),
    ("solid_organ_injury", "腹部实质性脏器损伤(通用)", TRAUMA, True, (PORTAL, ARTERIAL)),
    ("free_fluid_trauma", "创伤后腹腔游离积液", TRAUMA, True, (NONCONTRAST, PORTAL)),
]


def _build():
    table = {}
    for code, name, organ, emergency, protocols in _ENTRIES:
        if code in table:
            raise ValueError(f"目录条目重复: {code}")
        if organ not in ORGANS:
            raise ValueError(f"未知器官: {organ}")
        for protocol in protocols:
            if protocol not in PROTOCOLS:
                raise ValueError(f"未知协议: {protocol}")
        table[code] = {
            "code": code,
            "name": name,
            "organ": organ,
            "emergency": emergency,
            "protocols": tuple(protocols),
        }
    return table


CONDITIONS = _build()

EMERGENCY_CODES = frozenset(c for c, e in CONDITIONS.items() if e["emergency"])


def get(code):
    """返回病种条目 dict，未知返回 None。"""
    return CONDITIONS.get(code)


def supports_protocol(code, protocol):
    """该病种是否声明支持该扫描协议。"""
    entry = CONDITIONS.get(code)
    return entry is not None and protocol in entry["protocols"]


def is_emergency(code):
    entry = CONDITIONS.get(code)
    return bool(entry and entry["emergency"])
