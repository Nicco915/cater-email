from pathlib import Path
import hashlib
import pytest
from cater.app.response import validate_core_response
from cater.app.errors import ValidationError


@pytest.mark.parametrize('path_form', ['absolute', 'cwd_relative', 'filename'])
def test_artifact_paths_resolve_without_double_prefix(tmp_path, monkeypatch, path_form):
    monkeypatch.chdir(tmp_path)
    output = Path('runtime/test/tasks/task_sample/v2/outputs')
    output.mkdir(parents=True)
    artifact = output / 'result.xlsx'
    artifact.write_bytes(b'example')
    path = {'absolute': str(artifact.resolve()), 'cwd_relative': str(artifact),
            'filename': artifact.name}[path_form]
    response = {'status': 'ready', 'kind': 'parts', 'records': [{'record_key': 'x'}],
                'content_hash': 'hash', 'artifacts': [{'path': path, 'filename': artifact.name,
                'sha256': hashlib.sha256(b'example').hexdigest()}]}
    result = validate_core_response(response, output_dir=output)
    assert result['artifacts'][0]['path'] == str(artifact.resolve())
    response['artifacts'][0]['path'] = str(tmp_path / 'outside.xlsx')
    with pytest.raises(ValidationError, match='escapes'):
        validate_core_response(response, output_dir=output)
