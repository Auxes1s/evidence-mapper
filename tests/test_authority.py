from repo_research.promotion import authority_score

def test_authority_uses_document_content_not_parent_directory_name():
    briefer={"source_path":"Amendments/communications-briefer.txt","source_type":"txt",
             "excerpt":"This retrospective briefer summarizes the project.","context":"Presentation for discussion only.",
             "evidence_type":"retrospective","quote_verified":True}
    instrument={"source_path":"misc/scan-004.pdf","source_type":"pdf",
                "excerpt":"Signed by the parties. This Fourth Amendment enters into force on 4 May 2022.",
                "context":"IN WITNESS WHEREOF, the authorized representatives have signed this Amendment.",
                "evidence_type":"direct","quote_verified":True}
    assert authority_score(instrument) > authority_score(briefer) + 30
