# -*- coding: utf-8 -*-

# Copyright 2019 Tomoki Hayashi
#  MIT License (https://opensource.org/licenses/MIT)

"""STFT-based Loss modules."""

from distutils.version import LooseVersion

import torch
import torch.nn.functional as F
import torchaudio

is_pytorch_17plus = LooseVersion(torch.__version__) >= LooseVersion("1.7")


def stft(x, fft_size, hop_size, win_length, window):
    """Perform STFT and convert to magnitude spectrogram.

    Args:
        x (Tensor): Input signal tensor (B, T).
        fft_size (int): FFT size.
        hop_size (int): Hop size.
        win_length (int): Window length.
        window (str): Window function type.

    Returns:
        Tensor: Magnitude spectrogram (B, #frames, fft_size // 2 + 1).

    """
    window = window.cuda()
    if is_pytorch_17plus:
        x_stft = torch.stft(
            x, fft_size, hop_size, win_length, window, return_complex=False
        )
    else:
        x_stft = torch.stft(x, fft_size, hop_size, win_length, window)
    real = x_stft[..., 0]
    imag = x_stft[..., 1]

    # NOTE(kan-bayashi): clamp is needed to avoid nan or inf
    return torch.sqrt(torch.clamp(real**2 + imag**2, min=1e-7)).transpose(2, 1)


class SpectralConvergenceLoss(torch.nn.Module):
    """Spectral convergence loss module."""

    def __init__(self):
        """Initilize spectral convergence loss module."""
        super(SpectralConvergenceLoss, self).__init__()

    def forward(self, x_mag, y_mag):
        """Calculate forward propagation.

        Args:
            x_mag (Tensor): Magnitude spectrogram of predicted signal (B, #frames, #freq_bins).
            y_mag (Tensor): Magnitude spectrogram of groundtruth signal (B, #frames, #freq_bins).

        Returns:
            Tensor: Spectral convergence loss value.

        """
        delta_win_length = 5

        loss_1 = torch.norm(y_mag - x_mag, p="fro") / torch.norm(y_mag, p="fro")

        x_mag = x_mag.transpose(-1, -2)
        y_mag = y_mag.transpose(-1, -2)

        x_del = torchaudio.functional.compute_deltas(x_mag, win_length=delta_win_length)
        y_del = torchaudio.functional.compute_deltas(y_mag, win_length=delta_win_length)
        loss_2 = torch.norm(y_del - x_del, p="fro") / torch.norm(y_del, p="fro")

        x_acc = torchaudio.functional.compute_deltas(x_del, win_length=delta_win_length)
        y_acc = torchaudio.functional.compute_deltas(y_del, win_length=delta_win_length)
        loss_3 = torch.norm(y_acc - x_acc, p="fro") / torch.norm(y_acc, p="fro")

        return loss_1 + loss_2 + loss_3


class LogSTFTMagnitudeLoss(torch.nn.Module):
    """Log STFT magnitude loss module."""

    def __init__(self):
        """Initilize los STFT magnitude loss module."""
        super(LogSTFTMagnitudeLoss, self).__init__()

    def forward(self, x_mag, y_mag):
        """Calculate forward propagation.

        Args:
            x_mag (Tensor): Magnitude spectrogram of predicted signal (B, #frames, #freq_bins).
            y_mag (Tensor): Magnitude spectrogram of groundtruth signal (B, #frames, #freq_bins).

        Returns:
            Tensor: Log STFT magnitude loss value.

        """
        delta_win_length = 5

        loss_1 = F.l1_loss(torch.log(y_mag), torch.log(x_mag))

        x_mag = torch.log(x_mag).transpose(-1, -2)
        y_mag = torch.log(y_mag).transpose(-1, -2)

        x_del = torchaudio.functional.compute_deltas(x_mag, win_length=delta_win_length)
        y_del = torchaudio.functional.compute_deltas(y_mag, win_length=delta_win_length)
        loss_2 = F.l1_loss(y_del, x_del)

        x_acc = torchaudio.functional.compute_deltas(x_del, win_length=delta_win_length)
        y_acc = torchaudio.functional.compute_deltas(y_del, win_length=delta_win_length)
        loss_3 = F.l1_loss(y_acc, x_acc)

        return loss_1 + loss_2 + loss_3


class STFTLoss(torch.nn.Module):
    """STFT loss module."""

    def __init__(
        self, fft_size=1024, shift_size=120, win_length=600, window="hann_window"
    ):
        """Initialize STFT loss module."""
        super(STFTLoss, self).__init__()
        self.fft_size = fft_size
        self.shift_size = shift_size
        self.win_length = win_length
        self.spectral_convergence_loss = SpectralConvergenceLoss()
        self.log_stft_magnitude_loss = LogSTFTMagnitudeLoss()
        # NOTE(kan-bayashi): Use register_buffer to fix #223
        self.register_buffer("window", getattr(torch, window)(win_length))

    def forward(self, x, y):
        """Calculate forward propagation.

        Args:
            x (Tensor): Predicted signal (B, T).
            y (Tensor): Groundtruth signal (B, T).

        Returns:
            Tensor: Spectral convergence loss value.
            Tensor: Log STFT magnitude loss value.

        """
        x_mag = stft(x, self.fft_size, self.shift_size, self.win_length, self.window)
        y_mag = stft(y, self.fft_size, self.shift_size, self.win_length, self.window)
        sc_loss = self.spectral_convergence_loss(x_mag, y_mag)
        mag_loss = self.log_stft_magnitude_loss(x_mag, y_mag)

        return sc_loss, mag_loss


class MultiResolutionSTFTLoss(torch.nn.Module):
    """Multi resolution STFT loss module."""

    def __init__(
        self,
        fft_sizes=[1024, 2048, 512],
        hop_sizes=[120, 240, 50],
        win_lengths=[600, 1200, 240],
        window="hann_window",
    ):
        """Initialize Multi resolution STFT loss module.

        Args:
            fft_sizes (list): List of FFT sizes.
            hop_sizes (list): List of hop sizes.
            win_lengths (list): List of window lengths.
            window (str): Window function type.

        """
        super(MultiResolutionSTFTLoss, self).__init__()
        assert len(fft_sizes) == len(hop_sizes) == len(win_lengths)
        self.stft_losses = torch.nn.ModuleList()
        for fs, ss, wl in zip(fft_sizes, hop_sizes, win_lengths):
            self.stft_losses += [STFTLoss(fs, ss, wl, window)]

    def forward(self, y, x):
        """Calculate forward propagation.

        Args:
            x (Tensor): Predicted signal (B, T) or (B, #subband, T).
            y (Tensor): Groundtruth signal (B, T) or (B, #subband, T).

        Returns:
            Tensor: Multi resolution spectral convergence loss value.
            Tensor: Multi resolution log STFT magnitude loss value.

        """
        if len(x.shape) == 3:
            x = x.view(-1, x.size(2))  # (B, C, T) -> (B x C, T)
            y = y.view(-1, y.size(2))  # (B, C, T) -> (B x C, T)
        sc_loss = 0.0
        mag_loss = 0.0
        for f in self.stft_losses:
            sc_l, mag_l = f(x, y)
            sc_loss += sc_l
            mag_loss += mag_l
        sc_loss /= len(self.stft_losses)
        mag_loss /= len(self.stft_losses)

        return sc_loss + mag_loss
