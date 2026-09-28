import json
from pathlib import Path
from unittest.mock import patch

import pytest

from video_kb import extract, media, network, asr
from test_extraction import Connection, Response

SHORT = 'https://xhslink.cn/o/AbCdEf12345'
XHS = 'https://www.xiaohongshu.com/explore/abcdef1234567890abcdef12'
DOUYIN = 'https://www.douyin.com/video/123456789'
STREAM = 'https://sns-video-bd.xhscdn.com/stream/110/123.mp4?sign=example'


def page(note, extra=None):
    notes = {XHS.rsplit('/', 1)[-1]: {'note': note}}
    notes.update(extra or {})
    return '<script>window.__INITIAL_STATE__=' + json.dumps({'note': {'noteDetailMap': notes}}) + '</script>'


def video(url=STREAM):
    return {'noteId': XHS.rsplit('/', 1)[-1], 'title': 'Example', 'desc': '配文不是说话', 'type': 'video',
            'video': {'media': {'stream': {'h264': [{'masterUrl': url}]}}}}


def test_real_share_and_identity():
    assert network.validate_source_url(SHORT) == 'xiaohongshu'
    assert network.normalize_source_url(SHORT) == SHORT
    assert network.source_urls('这是一个能让电子小白省下上千块硬件投资的 ' + SHORT + ' 存好这段口令，去【小红书】逛逛吧~') == [SHORT]
    assert network.source_urls(SHORT) == [SHORT]
    assert network.source_identity(SHORT) is None
    assert network.normalize_source_url(XHS.replace('/explore/', '/discovery/item/') + '?xsec_token=abc') == XHS


@pytest.mark.parametrize('url', ['https://xhslink.cn.evil.test/o/AbCdEf12345', 'https://evil@xhslink.cn/o/AbCdEf12345', 'https://xhslink.cn/login', 'https://xhslink.cn/o/../secret', 'http://xhslink.cn/o/AbCdEf12345'])
def test_bad_short_links(url):
    with pytest.raises(network.SourceError):
        network.validate_source_url(url)


def test_redirect_anchor_stops_different_note():
    Connection.responses = [Response(status=302, headers={'Location': XHS}), Response(status=302, headers={'Location': XHS[:-1] + 'b'})]
    Connection.created = []
    with patch.object(network, 'resolve_public', return_value=('1.1.1.1',)), patch.object(network, '_PinnedHTTPS', Connection), pytest.raises(network.SourceError, match='不同内容'):
        network.fetch_source(SHORT)
    assert len(Connection.created) == 2


def test_signed_share_query_preserved_during_fetch():
    target = XHS.replace('/explore/', '/discovery/item/') + '?xsec_token=needed&xsec_source=pc_share'
    paths = []
    class SignedConnection(Connection):
        def request(self, method, path, headers):
            super().request(method, path, headers)
            paths.append(path)
    Connection.responses = [Response(status=302, headers={'Location': target}), Response()]
    with patch.object(network, 'resolve_public', return_value=('1.1.1.1',)), patch.object(network, '_PinnedHTTPS', SignedConnection):
        result = network.fetch_source(SHORT)
    assert 'xsec_token=needed' in paths[-1]
    assert result.url == target
    assert network.normalize_source_url(result.url) == XHS


@pytest.mark.parametrize('url', ['https://xhscdn.com.evil.test/a.mp4', 'https://127.0.0.1/a.mp4', 'http://sns-video-bd.xhscdn.com/a.mp4', 'https://sns-video-bd.xhscdn.com/a.m3u8', 'https://user:pw@sns-video-bd.xhscdn.com/a.mp4', 'https://v1.douyinvod.com/a.mp4'])
def test_media_host_separate_and_restricted(url):
    with pytest.raises(network.SourceError):
        media.validate_media_url(media.MediaRef(XHS, url))


def test_source_must_be_resolved_before_media():
    with pytest.raises(network.SourceError):
        media.validate_media_url(media.MediaRef(SHORT, STREAM))


def test_other_notes_media_never_selected(tmp_path):
    html = page({'type': 'video'}, {'other': {'note': video()}})
    with patch.object(extract, 'fetch_source', return_value=network.Fetched(XHS, html.encode(), 'text/html')), patch.object(extract, '_transcribe_media') as stt:
        card = extract.extract_url(XHS, cache_dir=tmp_path)
    stt.assert_not_called()
    assert card['evidence'] != 'asr'


def test_matching_media_calls_asr_in_real_extract_branch(tmp_path):
    html = page(video())
    with patch.object(extract, 'fetch_source', return_value=network.Fetched(XHS, html.encode(), 'text/html')), patch.object(extract, '_transcribe_media', return_value={'text': '实际音轨分支返回', 'audio_seconds': 42, 'truncated': False}) as stt:
        card = extract.extract_url(SHORT, cache_dir=tmp_path)
    assert stt.call_args.args[0] == media.MediaRef(XHS, STREAM)
    assert card['evidence'] == 'asr' and card['source_text'] == '实际音轨分支返回'
    assert '非官方字幕' in card['evidence_note']


def test_short_audio_marks_partial_but_silence_does_not(tmp_path):
    html = page(video())
    for mismatch in (True, False):
        data = {'text': '实际讲话', 'audio_seconds': 10 if mismatch else 120,
                'media_seconds': 120, 'duration_mismatch': mismatch, 'last_speech_seconds': 8, 'truncated': False}
        with patch.object(extract, 'fetch_source', return_value=network.Fetched(XHS, html.encode(), 'text/html')), patch.object(extract, '_transcribe_media', return_value=data):
            card = extract.extract_url(XHS, cache_dir=tmp_path)
        assert card['evidence'] == ('partial' if mismatch else 'asr')
        assert ('时长存在明显差异' in card['evidence_note']) == mismatch


def test_unresolved_short_link_cannot_supply_transcript(tmp_path):
    short = 'https://v.douyin.com/Example/'
    html = '<script>' + json.dumps({'@context': 'https://schema.org', '@type': 'VideoObject', 'url': short, 'transcript': '无法归属'}) + '</script>'
    with patch.object(extract, 'fetch_source', return_value=network.Fetched(short, html.encode(), 'text/html')):
        card = extract.extract_url(short, cache_dir=tmp_path)
    assert card['evidence'] != 'asr' and card['source_text'] == ''


def test_missing_asr_is_retryable_not_a_saved_bookmark(tmp_path):
    html = page(video())
    with patch.object(extract, 'fetch_source', return_value=network.Fetched(XHS, html.encode(), 'text/html')), patch.object(extract, 'check_asr', return_value={'available': False, 'message': 'missing local dependency'}), pytest.raises(network.SourceError, match='missing local dependency'):
        extract.extract_url(XHS, cache_dir=tmp_path)


def test_captions_avoid_download(tmp_path):
    html = page(video()) + '<script>' + json.dumps({'@context': 'https://schema.org', '@type': 'VideoObject', 'url': XHS, 'transcript': '原生文字稿'}) + '</script>'
    with patch.object(extract, 'fetch_source', return_value=network.Fetched(XHS, html.encode(), 'text/html')), patch.object(extract, '_transcribe_media') as stt:
        card = extract.extract_url(XHS, cache_dir=tmp_path)
    stt.assert_not_called()
    assert card['source_text'] == '原生文字稿'


@pytest.mark.parametrize('transcript_url,same_source', [
    (XHS.replace('/explore/', '/discovery/item/') + '?xsec_token=shared', True),
    (XHS[:-1] + 'b', False),
    (DOUYIN, False),
])
def test_xhs_schema_only_transcript_requires_same_source(tmp_path, transcript_url, same_source):
    html = '<meta property="og:title" content="视频"><script>' + json.dumps({
        '@context': 'https://schema.org', '@type': 'VideoObject',
        'url': transcript_url, 'transcript': '与本条视频关联的公开文字稿',
    }) + '</script>'
    with patch.object(extract, 'fetch_source', return_value=network.Fetched(XHS, html.encode(), 'text/html')), patch.object(extract, '_transcribe_media') as stt:
        card = extract.extract_url(SHORT, cache_dir=tmp_path)
    stt.assert_not_called()
    if same_source:
        assert card['content_type'] == 'video'
        assert card['source_text'] == '与本条视频关联的公开文字稿'
        assert card['evidence'] == 'partial'
        assert '未验证是否覆盖' in card['evidence_note']
    else:
        assert card['source_text'] == ''
        assert card['evidence'] == 'metadata'


def test_douyin_requested_item_only():
    wrong = {'aweme_id': '88888888', 'video': {'play_addr': {'url_list': ['https://v1.douyinvod.com/wrong.mp4']}}}
    right = {'aweme_id': '123456789', 'video': {'play_addr': {'url_list': ['https://v1.douyinvod.com/right.mp4']}}}
    data = 'window._ROUTER_DATA=' + json.dumps({'loaderData': {'item_list': [wrong, right]}})
    selected = extract._douyin_item([data], DOUYIN)
    assert extract._media_ref('douyin', DOUYIN, selected).url.endswith('/right.mp4')


def test_media_redirect_private_and_truncated_removed(tmp_path):
    for response in [Response(status=302, headers={'Location': 'https://127.0.0.1/secret'}), Response(body=b'xxxxftypisom', headers={'Content-Type': 'video/mp4', 'Content-Length': '100'})]:
        Connection.responses = [response]
        Connection.created = []
        destination = tmp_path / 'download.mp4'
        with patch.object(media, 'resolve_public', return_value=('1.1.1.1',)), patch.object(media, '_PinnedHTTPS', Connection), pytest.raises(network.SourceError):
            media.download_media(media.MediaRef(XHS, STREAM), destination)
        assert not destination.exists()


def test_binary_magic_and_size_bound(tmp_path):
    for body, headers in [(b'<html>login</html>', {'Content-Type': 'video/mp4'}), (b'', {'Content-Type': 'video/mp4', 'Content-Length': str(media.MAX_MEDIA_BYTES + 1)}), (b'xxxxftypisom', {'Content-Type': 'text/html'})]:
        Connection.responses = [Response(body=body, headers=headers)]
        with patch.object(media, 'resolve_public', return_value=('1.1.1.1',)), patch.object(media, '_PinnedHTTPS', Connection), pytest.raises(network.SourceError):
            media.download_media(media.MediaRef(XHS, STREAM), tmp_path / 'download.mp4')


def test_existing_media_file_is_preserved(tmp_path):
    path = tmp_path / 'download.mp4'
    path.write_bytes(b'user file')
    with pytest.raises(network.SourceError):
        media.download_media(media.MediaRef(XHS, STREAM), path)
    assert path.read_bytes() == b'user file'


def test_dead_owned_cache_removed_unknown_preserved(tmp_path):
    cache = tmp_path / 'cache'
    cache.mkdir(mode=0o700)
    owned = cache / 'media-owned'
    owned.mkdir(mode=0o700)
    (owned / 'owner.json').write_text(json.dumps({'owner': 'hermes-video-kb-media-v1', 'pid': 88888}))
    (owned / 'source.mp4').write_bytes(b'download')
    unknown = cache / 'media-unknown'
    unknown.mkdir(mode=0o700)
    (unknown / 'user.txt').write_text('keep')
    with patch.object(media.os, 'kill', side_effect=ProcessLookupError):
        media.prepare_cache(cache)
    assert not owned.exists()
    assert (unknown / 'user.txt').read_text() == 'keep'


def test_cache_symlink_refused_without_deletion(tmp_path):
    cache = tmp_path / 'cache'
    cache.mkdir(mode=0o700)
    target = tmp_path / 'user'
    target.write_bytes(b'user')
    (cache / 'media-bad').symlink_to(target)
    with pytest.raises(network.SourceError, match='符号链接'):
        media.prepare_cache(cache)
    assert target.read_bytes() == b'user'


def test_local_subprocess_does_not_inherit_keys(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'never-pass')
    monkeypatch.setenv('HTTPS_PROXY', 'never-pass')
    env = media.local_environment()
    assert 'OPENAI_API_KEY' not in env and 'HTTPS_PROXY' not in env
    assert env['HF_HUB_OFFLINE'] == '1'


def test_no_model_download_during_discovery(tmp_path):
    model = tmp_path / 'model'
    model.mkdir()
    for name in asr.REQUIRED_MODEL_FILES:
        (model / name).write_bytes(b'x')
    assert asr.cached_model({'local_asr_model': str(model)}) == model.resolve()
    (model / 'model.bin').unlink()
    assert asr.cached_model({'local_asr_model': str(model)}) is None
