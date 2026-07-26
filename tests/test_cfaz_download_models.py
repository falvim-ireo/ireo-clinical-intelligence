import zipfile
from acquisition.cfaz_download_models import validate_downloads, CfazDownloadModelsError

def test_validate_two_distinct_stl_zips(tmp_path):
    paths=[]
    for i, data in enumerate((b"solid a", b"solid b")):
        p=tmp_path/f"{i}.zip"
        with zipfile.ZipFile(p,"w") as z: z.writestr("model.stl", data)
        paths.append(p)
    result=validate_downloads(paths)
    assert len(result)==2 and result[0][0]!=result[1][0]

def test_duplicate_content_rejected(tmp_path):
    paths=[]
    for i in range(2):
        p=tmp_path/f"{i}.zip"
        with zipfile.ZipFile(p,"w") as z: z.writestr("model.stl", b"same")
        paths.append(p)
    try: validate_downloads(paths)
    except CfazDownloadModelsError: pass
    else: raise AssertionError("duplicate should fail")
