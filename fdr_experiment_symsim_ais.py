import os

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
from arviz.stats import psislw

from scvi.ais import ais_trajectory, ais_trajectory_sample
from scvi.inference import UnsupervisedTrainer
from scvi.models import VAE

from fdr_experiment_symsim import (
    DATASET,
    IS_SIGNIFICANT_DE,
    SAMPLE_IDX,
    Y,
    get_predictions_is,
    posterior_expected_fdr,
    precision_score,
    recall_score,
    true_fdr,
)

N_EPOCHS = 200  # High number of epochs sounds vital to reach acceptable levels of khat
LR = 1e-3
TRAIN_SIZE = 0.8
BATCH_SIZE = 128

N_HIDDEN_ARR = [128]

# Params for FDR measurements
N_PICKS = 5
N_GENES = DATASET.nb_genes


# Training model
mdl = VAE(n_input=N_GENES, prevent_library_saturation=False, n_latent=10,)

PATH = "baseline.pt"
if os.path.exists(PATH):
    mdl.load_state_dict(torch.load(PATH))
    mdl.cuda()

trainer = UnsupervisedTrainer(
    model=mdl, gene_dataset=DATASET, frequency=10, batch_size=128
)
if not os.path.exists(PATH):
    trainer.train(
        n_epochs=200,
        # n_epochs=300,
        lr=1e-3,
        eps=0.01,
        wake_theta="ELBO",
        wake_psi="ELBO",
        n_samples_theta=1,
        n_samples_phi=1,
        do_observed_library=True,
    )

# Khat
post = trainer.create_posterior(
    model=mdl, gene_dataset=DATASET, indices=SAMPLE_IDX
).sequential(batch_size=2)
n_post_samples = 100
zs, logws = ais_trajectory(model=mdl, loader=post, n_sample=n_post_samples)

cubo = 0.5 * torch.logsumexp(2 * logws, dim=0) - np.log(n_post_samples)
iwelbo = torch.logsumexp(logws, dim=0) - np.log(n_post_samples)
_, khats = psislw(logws.T)

# Mus
test_indices = trainer.train_set.indices
y_test = Y[test_indices]
decision_rule_fdr10 = np.zeros(N_PICKS)
decision_rule_tpr10 = np.zeros(N_PICKS)
fdr_gt = np.zeros((N_GENES, N_PICKS))
pe_fdr = np.zeros((N_GENES, N_PICKS))
n_post_samples = 100

for ipick in range(N_PICKS):
    samples_a = np.random.choice(np.where(y_test == 1)[0], size=10)
    samples_b = np.random.choice(np.where(y_test == 2)[0], size=10)

    where_a = test_indices[samples_a]
    where_b = test_indices[samples_b]
    post_a = trainer.create_posterior(
        model=mdl, gene_dataset=DATASET, indices=where_a
    ).sequential(batch_size=2)

    post_b = trainer.create_posterior(
        model=mdl, gene_dataset=DATASET, indices=where_b
    ).sequential(batch_size=2)

    z_a, logwa = ais_trajectory(model=mdl, loader=post_a, n_sample=n_post_samples)
    z_b, logwb = ais_trajectory(model=mdl, loader=post_b, n_sample=n_post_samples)
    # Shapes n_samples, n_batch, n_latent
    # and n_samples, n_batch
    softmax = nn.Softmax(dim=0)

    with torch.no_grad():
        log_h_a = (
            mdl.decoder(
                mdl.dispersion, z_a.cuda(), torch.ones(len(z_a), 10, 1, device="cuda"), None
            )[0]
            .log2()
            .cpu()
            .view(n_post_samples, 1, 10, -1)
        )
        log_h_b = (
            mdl.decoder(
                mdl.dispersion, z_b.cuda(), torch.ones(len(z_b), 10, 1, device="cuda"), None
            )[0]
            .log2()
            .cpu()
            .view(1, n_post_samples, 10, -1)
        )
        w_a = softmax(logwa).view(n_post_samples, 1, 10, 1)
        w_b = softmax(logwb).view(1, n_post_samples, 10, 1)

        y_pred_is = ((log_h_a - log_h_b).abs() >= 0.5).float()
        y_pred_is = y_pred_is * w_a * w_b
        y_pred_is = y_pred_is.sum([0, 1]).mean(0)

    y_pred_is = y_pred_is.numpy()

    true_fdr_arr = true_fdr(y_true=IS_SIGNIFICANT_DE, y_pred=y_pred_is)
    pe_fdr_arr, y_decision_rule = posterior_expected_fdr(y_pred=y_pred_is)
    # Fdr related
    fdr_gt[:, ipick] = true_fdr_arr
    pe_fdr[:, ipick] = pe_fdr_arr

    _, y_decision_rule10 = posterior_expected_fdr(y_pred=y_pred_is, fdr_target=0.1)
    decision_rule_fdr10[ipick] = 1 - precision_score(
        y_true=IS_SIGNIFICANT_DE, y_pred=y_decision_rule10
    )
    decision_rule_tpr10[ipick] = recall_score(
        y_true=IS_SIGNIFICANT_DE, y_pred=y_decision_rule10
    )

# all_fdr_gt.append(fdr_gt)
# all_pe_fdr.append(pe_fdr)
# fdr_controlled_fdr10.append(decision_rule_fdr10)
# fdr_controlled_tpr10.append(decision_rule_tpr10)

res = {
    "khat_10000": khats,
    "cubo": cubo,
    "iwelbo": iwelbo,
    "fdr_controlled_fdr10": decision_rule_fdr10,
    "fdr_controlled_tpr10": decision_rule_tpr10,
}
all_res = [res]
df = pd.DataFrame(all_res)
df.to_pickle("IAS_symsim.pkl")
