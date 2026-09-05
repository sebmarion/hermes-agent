"""Our terminal-output identity must never be reassigned by matching text."""
import json
import pytest
from hermes_state import SessionDB

@pytest.mark.parametrize("field,new", [("delivery_id","new-delivery"),("delegation_id","new-job")])
def test_terminal_identity_cannot_be_overwritten_by_equal_text(tmp_path, field, new):
    db=SessionDB(tmp_path/'state.db')
    try:
        db.create_session('s',source='cli')
        before={"delivery_id":"original","delegation_id":"original-job"}
        db.append_message('s','assistant','same text',display_metadata=before)
        with pytest.raises(ValueError, match="identity"):
            db.set_latest_matching_message_display_metadata('s',role='assistant',content='same text',
                display_metadata={field:new})
        row=db.get_messages('s')[-1]
        value=row['display_metadata'];value=json.loads(value) if isinstance(value,str) else value
        assert value==before
        assert db.set_latest_matching_message_display_metadata('s',role='assistant',content='same text',
            display_metadata=before)
    finally: db.close()
