"""Leakage/causality checks for the stateful manual-only research pipeline."""
import numpy as np
import pandas as pd
import pytest
from scripts.tune_manual_models import (
    load_rich_data, optical_features, causal_ema, _fit_floors,
    RAW_LIGHT_COLUMNS, SIGNED, threshold_options, session_weights,
)


@pytest.fixture(scope="module")
def optical_stream():
    frames,_=load_rich_data()
    ids=frames.session_id.unique()[:2]
    sample=pd.concat([frames[frames.session_id==sid].iloc[:24] for sid in ids],ignore_index=True)
    return sample,_fit_floors(frames[frames.group.isin([1,2,3,4])])


def test_future_optical_changes_cannot_affect_earlier_features(optical_stream):
    frames,floors=optical_stream
    changed=frames.copy()
    changed.loc[12:23,RAW_LIGHT_COLUMNS+SIGNED] *= 10
    before,after=optical_features(frames,floors),optical_features(changed,floors)
    for pack in before:
        np.testing.assert_array_equal(before[pack][:12],after[pack][:12])
        np.testing.assert_array_equal(before[pack][24:],after[pack][24:])
    assert not np.allclose(before["temporal"][12:24],after["temporal"][12:24])


def test_reference_and_target_metadata_do_not_enter_features(optical_stream):
    frames,floors=optical_stream
    changed=frames.copy()
    changed["reference_force_corrected_N"]=9999
    changed["target_roi"]=99
    changed["test_group"]="TEST999"
    changed["group"]=999
    before,after=optical_features(frames,floors),optical_features(changed,floors)
    for pack in before:np.testing.assert_array_equal(before[pack],after[pack])


def test_session_boundary_resets_history(optical_stream):
    frames,floors=optical_stream
    whole=optical_features(frames,floors)
    second=optical_features(frames.iloc[24:],floors)
    for pack in whole:np.testing.assert_array_equal(whole[pack][24:],second[pack])


def test_output_history_is_causal_and_resets_after_gap():
    ids=np.array(["a"]*5+["b"]*2)
    times=np.array([0,.1,.2,.8,.9,0,.1])
    x=np.array([0,1,2,3,4,8,9],dtype=float)
    y=causal_ema(x,ids,times,.3)
    np.testing.assert_array_equal(y[:3],causal_ema(x[:3],ids[:3],times[:3],.3))
    assert y[3]==x[3] and y[5]==x[5]


def test_threshold_search_respects_ties_and_fpr_constraint():
    truth=np.array([0,0,0,0,1,1,1,1])
    score=np.array([.1,.2,.3,.5,.4,.5,.8,.9])
    w=np.ones(8)
    choices=dict(threshold_options(truth,score,w))
    pred=score>=choices["fpr5"]
    assert pred[truth==0].mean()<=.05
    assert pred[truth==1].mean()==.5
    balanced=lambda t: .5*((score[truth==1]>=t).mean()+(score[truth==0]<t).mean())
    assert balanced(choices["balanced"])==max(balanced(t) for t in np.r_[score,1.1])
