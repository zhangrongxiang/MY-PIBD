from ast import Lambda
import numpy as np
import os
from custom_optims.radam import RAdam
from models.model_PIBD import PIBD
from sksurv.metrics import concordance_index_censored, concordance_index_ipcw, brier_score, integrated_brier_score, cumulative_dynamic_auc
from sksurv.util import Surv
from utils.file_utils import _save_pkl

#----> pytorch imports
import torch

from utils.general_utils import _get_split_loader, _print_network, _save_splits
from utils.loss_func import NLLSurvLoss,SurvPLE, RankLoss

import torch.optim as optim



def _get_splits(datasets, cur, args):
    r"""
    Summarize the train and val splits and return them individually
    
    Args:
        - datasets : tuple
        - cur : Int 
        - args: argspace.Namespace
    
    Return:
        - train_split : SurvivalDataset
        - val_split : SurvivalDataset
    
    """

    print('\nTraining Fold {}!'.format(cur))
    print('\nInit train/val splits...', end=' ')
    train_split, val_split = datasets
    _save_splits(datasets, ['train', 'val'], os.path.join(args.results_dir, 'splits_{}.csv'.format(cur)))
    print('Done!')
    print("Training on {} samples".format(len(train_split)))
    print("Validating on {} samples".format(len(val_split)))

    return train_split,val_split


def _init_loss_function(args):
    r"""
    Init the survival loss function
    
    Args:
        - args : argspace.Namespace 
    
    Returns:
        - loss_fn : NLLSurvLoss or NLLRankSurvLoss
    
    """
    print('\nInit loss function...', end=' ')
    if args.bag_loss == 'nll_surv':
        loss_fn = NLLSurvLoss(alpha=args.alpha_surv)
    elif args.bag_loss == 'cox_surv':
        loss_fn = SurvPLE()
    elif args.bag_loss == 'rank_surv':
        loss_fn = RankLoss()
    else:
        raise NotImplementedError
    print('Done!')
    return loss_fn

def _init_optim(args, model):
    r"""
    Init the optimizer 
    
    Args: 
        - args : argspace.Namespace 
        - model : torch model 
    
    Returns:
        - optimizer : torch optim 
    """
    print('\nInit optimizer ...', end=' ')

    if args.opt == "adam":
        optimizer = optim.Adam(model.parameters(), lr=args.lr)
    elif args.opt == 'sgd':
        optimizer = optim.SGD(model.parameters(), lr=args.lr, momentum=0.9, weight_decay=args.reg)
    elif args.opt == "adamW":
        optimizer = optim.AdamW(model.parameters(), lr=args.lr, weight_decay=args.reg)
    elif args.opt == "radam":
        optimizer = RAdam(model.parameters(), lr=args.lr, weight_decay=args.reg)
    elif args.opt == "lamb":
        optimizer = Lambda(model.parameters(), lr=args.lr, weight_decay=args.reg)
    else:
        raise NotImplementedError

    return optimizer

def _init_model(args):
    r"""
    Init the model
    Args:
        - args : argspace.Namespace

    Returns:
        - model : torch model
    """
    
    print('\nInit Model...', end=' ')
    if args.type_of_path == "xena":
        omics_input_dim = 1577
    elif args.type_of_path == "hallmarks":
        omics_input_dim = 4241
    elif args.type_of_path == "combine":
        omics_input_dim = 4999
    elif args.type_of_path == "multi":
        if args.study == "tcga_brca":
            omics_input_dim = 9947
        else:
            omics_input_dim = 14933
    else:
        omics_input_dim = 0

    if args.method == "PIBD":
        model_dict = {'args': args,
                      'omics_input_dim': omics_input_dim,}
        model = PIBD(**model_dict)

    else:
        print('Model not implemented in this version')
        raise NotImplementedError

    if torch.cuda.is_available():
        model = model.to(torch.device('cuda'))

    print('Done!')
    _print_network(args.results_dir, model)

    return model

def _init_loaders(args, train_split, val_split):
    r"""
    Init dataloaders for the train and val datasets 

    Args:
        - args : argspace.Namespace 
        - train_split : SurvivalDataset 
        - val_split : SurvivalDataset 
    
    Returns:
        - train_loader : Pytorch Dataloader 
        - val_loader : Pytorch Dataloader

    """

    print('\nInit Loaders...', end=' ')
    if train_split:
        train_loader = _get_split_loader(args, train_split, training=True, testing=False, weighted=args.weighted_sample, batch_size=args.batch_size)
    else:
        train_loader = None

    if val_split:
        val_loader = _get_split_loader(args, val_split,  testing=False, batch_size=1)
    else:
        val_loader = None
    print('Done!')

    return train_loader,val_loader

def _extract_survival_metadata(train_loader, val_loader):
    r"""
    Extract censorship and survival times from the train and val loader and combine to get numbers for the fold
    We need to do this for train and val combined because when evaulating survival metrics, the function needs to know the 
    distirbution of censorhsip and survival times for the trainig data
    
    Args:
        - train_loader : Pytorch Dataloader
        - val_loader : Pytorch Dataloader
    
    Returns:
        - all_survival : np.array
    
    """

    all_censorships = np.concatenate(
        [train_loader.dataset.metadata[train_loader.dataset.censorship_var].to_numpy(),
        val_loader.dataset.metadata[val_loader.dataset.censorship_var].to_numpy()],
        axis=0)

    all_event_times = np.concatenate(
        [train_loader.dataset.metadata[train_loader.dataset.label_col].to_numpy(),
        val_loader.dataset.metadata[val_loader.dataset.label_col].to_numpy()],
        axis=0)

    all_survival = Surv.from_arrays(event=(1-all_censorships).astype(bool), time=all_event_times)
    return all_survival

def _unpack_data(omics_format, device, data):
    r"""
    Depending on the model type, unpack the data and put it on the correct device
    
    Args:
        - omics_format : String
        - device : torch.device 
        - data : tuple 
    
    Returns:
        - data_WSI : torch.Tensor
        - mask : torch.Tensor
        - y_disc : torch.Tensor
        - event_time : torch.Tensor
        - censor : torch.Tensor
        - data_omics : torch.Tensor
        - clinical_data_list : list
        - mask : torch.Tensor
    
    """
    # prepare wsi data
    data_WSI = data[0].to(device)

    # prepare omics data
    if omics_format == "gene":
        mask = None
        data_omics = data[1].to(device)
        y_disc, event_time, censor, clinical_data_list = data[2], data[3], data[4], data[5]

    elif omics_format == "groups":

        data_omic1 = data[1].type(torch.FloatTensor).to(device)
        data_omic2 = data[2].type(torch.FloatTensor).to(device)
        data_omic3 = data[3].type(torch.FloatTensor).to(device)
        data_omic4 = data[4].type(torch.FloatTensor).to(device)
        data_omic5 = data[5].type(torch.FloatTensor).to(device)
        data_omic6 = data[6].type(torch.FloatTensor).to(device)
        data_omics = [data_omic1, data_omic2, data_omic3, data_omic4, data_omic5, data_omic6]

        y_disc, event_time, censor, clinical_data_list, mask = data[7], data[8], data[9], data[10], data[11]
        mask = mask.to(device)

    elif omics_format == "pathways":

        data_omics = []
        for idx,item in enumerate(data[1]):
            for idy,omic in enumerate(item):
                omic = omic.to(device)
                omic = omic.unsqueeze(0)
                if idx == 0:
                    data_omics.append(omic)
                else:
                    data_omics[idy] = torch.cat((data_omics[idy],omic),dim=0)
        
        if data[6][0,0] == 1:
            mask = None
        else:
            mask = data[6].to(device)

        y_disc, event_time, censor, clinical_data_list = data[2], data[3], data[4], data[5]
        
    else:
        raise ValueError('Unsupported omics type:', omics_format)
    
    y_disc, event_time, censor = y_disc.to(device), event_time.to(device), censor.to(device)

    return data_WSI, mask, y_disc, event_time, censor, data_omics, clinical_data_list, mask

def _process_data_and_forward(model, omics_format, device, data):
    r"""
    Depeding on the omics farmat, process the input data and do a forward pass on the model
    
    Args:
        - model : Pytorch model
        - omics_format : String
        - device : torch.device
        - data : tuple
    
    Returns:
        - out : torch.Tensor
        - y_disc : torch.Tensor
        - event_time : torch.Tensor
        - censor : torch.Tensor
        - clinical_data_list : List
    
    """
    data_WSI, mask, y_disc, event_time, censor, data_omics, clinical_data_list, mask = _unpack_data(omics_format, device, data)

    input_args = {"x_wsi": data_WSI.to(device)}
    input_args["return_attn"] = False
    input_args["y"] = y_disc
    input_args["c"] = censor

    if omics_format == "gene":

        input_args["x_omics"] = data_omics.to(device)
        
        out = model(**input_args)

    elif omics_format in ["groups", "pathways"]:

        for i in range(len(data_omics)):
            input_args['x_omic%s' % str(i+1)] = data_omics[i].type(torch.FloatTensor).to(device)

        out = model(**input_args)

    else:
        raise ValueError('Unsupported omics type:', omics_format)

    return out, y_disc, event_time, censor, clinical_data_list


def _calculate_risk(h):
    r"""
    Take the logits of the model and calculate the risk for the patient 
    
    Args: 
        - h : torch.Tensor 
    
    Returns:
        - risk : torch.Tensor 
    
    """
    hazards = torch.sigmoid(h)
    survival = torch.cumprod(1 - hazards, dim=1)
    risk = -torch.sum(survival, dim=1).detach().cpu().numpy()
    return risk, survival.detach().cpu().numpy()

def _update_arrays(all_risk_scores, all_censorships, all_event_times, all_clinical_data, event_time, censor, risk, clinical_data_list):
    r"""
    Update the arrays with new values 
    
    Args:
        - all_risk_scores : List
        - all_censorships : List
        - all_event_times : List
        - all_clinical_data : List
        - event_time : torch.Tensor
        - censor : torch.Tensor
        - risk : torch.Tensor
        - clinical_data_list : List
    
    Returns:
        - all_risk_scores : List
        - all_censorships : List
        - all_event_times : List
        - all_clinical_data : List
    
    """
    all_risk_scores.append(risk)
    all_censorships.append(censor.detach().cpu().numpy())
    all_event_times.append(event_time.detach().cpu().numpy())
    all_clinical_data.append(clinical_data_list)
    return all_risk_scores, all_censorships, all_event_times, all_clinical_data

def _train_loop_survival(args, epoch, model, omics_format, loader, optimizer, loss_fn, log_file):
    r"""
    Perform one epoch of training 

    Args:
        - args : Namespace
        - epoch : Int
        - model : Pytorch model
        - omics_format : String
        - loader : Pytorch dataloader
        - optimizer : torch.optim
        - loss_fn : custom loss function class
        - log_file : File
    
    Returns:
        - c_index : Float
        - total_loss : Float 
    
    """
    device=torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model.train()

    total_loss = 0.
    
    all_risk_scores = []
    all_censorships = []
    all_event_times = []
    all_clinical_data = []

    # one epoch
    for batch_idx, data in enumerate(loader):

        h, y_disc, event_time, censor, clinical_data_list = _process_data_and_forward(model, omics_format, device, data)


        logits, IB_loss_proxy, proxy_loss, mimin_total, mimin_loss_total = h[0], h[1], h[2], h[3], h[4]

        loss_surv = loss_fn(h=logits, y=y_disc, t=event_time, c=censor)

        loss = loss_surv + args.gamma * proxy_loss + IB_loss_proxy + args.sigma * (mimin_total + mimin_loss_total)
        # print("loss_surv:{},proxy_loss:{},IB_loss_proxy:{}".format(loss_surv.item(),proxy_loss.item(),IB_loss_proxy.item()))
        h = logits

        loss_value = loss.item()
        loss = loss / y_disc.shape[0]
        
        risk, _ = _calculate_risk(h)

        all_risk_scores, all_censorships, all_event_times, all_clinical_data = _update_arrays(all_risk_scores, all_censorships, all_event_times,all_clinical_data, event_time, censor, risk, clinical_data_list)

        total_loss += loss_value

        loss.backward()
        optimizer.step()
        optimizer.zero_grad()

        if (batch_idx % 1) == 0:
            print("batch: {}, loss: {:.3f}".format(batch_idx, loss.item()))
            log_file.write("batch: {}, loss: {:.3f}\n".format(batch_idx, loss.item()))
    
    total_loss /= len(loader.dataset)
    all_risk_scores = np.concatenate(all_risk_scores, axis=0)
    all_censorships = np.concatenate(all_censorships, axis=0)
    all_event_times = np.concatenate(all_event_times, axis=0)
    c_index = concordance_index_censored((1-all_censorships).astype(bool), all_event_times, all_risk_scores, tied_tol=1e-08)[0]

    print('Epoch: {}, train_loss: {:.4f}, train_c_index: {:.4f}'.format(epoch, total_loss, c_index))
    log_file.write('Epoch: {}, train_loss: {:.4f}, train_c_index: {:.4f}\n'.format(epoch, total_loss, c_index))


    return c_index, total_loss

def _calculate_metrics(loader, dataset_factory, survival_train, all_risk_scores, all_censorships, all_event_times, all_risk_by_bin_scores):
    r"""
    Calculate various survival metrics 
    
    Args:
        - loader : Pytorch dataloader
        - dataset_factory : SurvivalDatasetFactory
        - survival_train : np.array
        - all_risk_scores : np.array
        - all_censorships : np.array
        - all_event_times : np.array
        - all_risk_by_bin_scores : np.array

        
    Returns:
        - c_index : Float
        - c_index_ipcw : Float
        - BS : np.array
        - IBS : Float
        - iauc : Float
    
    """
    
    data = loader.dataset.metadata["survival_months_dss"]
    bins_original = dataset_factory.bins
    which_times_to_eval_at = np.array([data.min() + 0.0001, bins_original[1], bins_original[2], data.max() - 0.0001])

    #---> delete the nans and corresponding elements from other arrays 
    original_risk_scores = all_risk_scores
    all_risk_scores = np.delete(all_risk_scores, np.argwhere(np.isnan(original_risk_scores)))
    all_censorships = np.delete(all_censorships, np.argwhere(np.isnan(original_risk_scores)))
    all_event_times = np.delete(all_event_times, np.argwhere(np.isnan(original_risk_scores)))
    #<---

    c_index = concordance_index_censored((1-all_censorships).astype(bool), all_event_times, all_risk_scores, tied_tol=1e-08)[0]
    c_index_ipcw, BS, IBS, iauc = 0., 0., 0., 0.

    # change the datatype of survival test to calculate metrics 
    try:
        survival_test = Surv.from_arrays(event=(1-all_censorships).astype(bool), time=all_event_times)
    except:
        print("Problem converting survival test datatype, so all metrics 0.")
        return c_index, c_index_ipcw, BS, IBS, iauc
   
    # cindex2 (cindex_ipcw)
    try:
        c_index_ipcw = concordance_index_ipcw(survival_train, survival_test, estimate=all_risk_scores)[0]
    except:
        print('An error occured while computing c-index ipcw')
        c_index_ipcw = 0.
    
    # brier score 
    try:
        _, BS = brier_score(survival_train, survival_test, estimate=all_risk_by_bin_scores, times=which_times_to_eval_at)
    except:
        print('An error occured while computing BS')
        BS = 0.
    
    # IBS
    try:
        IBS = integrated_brier_score(survival_train, survival_test, estimate=all_risk_by_bin_scores, times=which_times_to_eval_at)
    except:
        print('An error occured while computing IBS')
        IBS = 0.

    # iauc
    try:
        _, iauc = cumulative_dynamic_auc(survival_train, survival_test, estimate=1-all_risk_by_bin_scores[:, 1:], times=which_times_to_eval_at[1:])
    except:
        print('An error occured while computing iauc')
        iauc = 0.
    
    return c_index, c_index_ipcw, BS, IBS, iauc


def _summary(dataset_factory, model, omics_format, loader, loss_fn, survival_train=None,miss=None):
    r"""
    Run a validation loop on the trained model 
    
    Args:
        - dataset_factory : SurvivalDatasetFactory
        - model : Pytorch model
        - omics_format : String
        - loader : Pytorch loader
        - loss_fn : custom loss function clas
        - survival_train : np.array
    
    Returns:
        - patient_results : dictionary
        - c_index : Float
        - c_index_ipcw : Float
        - BS : List
        - IBS : Float
        - iauc : Float
        - total_loss : Float

    """
    device=torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model.eval()

    total_loss = 0.

    all_risk_scores = []
    all_risk_by_bin_scores = []
    all_censorships = []
    all_event_times = []
    all_clinical_data = []
    all_logits = []
    all_slide_ids = []

    slide_ids = loader.dataset.metadata['slide_id']
    count = 0
    with torch.no_grad():

        for data in loader:

            data_WSI, mask, y_disc, event_time, censor, data_omics, clinical_data_list, mask = _unpack_data(omics_format, device, data)

            if miss=="P":
                data_WSI = torch.zeros_like(data_WSI).to(device)
            if miss=="G":
                if isinstance(data_omics, list):
                    # 如果 data_omics 是 list，则需要逐个 tensor 处理
                    data_omics = [torch.zeros_like(omic).to(device) for omic in data_omics]
                else:
                    # 否则直接转换
                    data_omics = torch.zeros_like(data_omics).to(device)
            input_args = {"x_wsi": data_WSI.to(device)}
            input_args["return_attn"] = False
            input_args["y"] = None
            input_args["c"] = None

            if omics_format == "gene":

                input_args["x_omics"] = data_omics.to(device)

            elif omics_format in ["groups", "pathways"]:

                for i in range(len(data_omics)):
                    input_args['x_omic%s' % str(i + 1)] = data_omics[i].type(torch.FloatTensor).to(device)

            else:
                raise NotImplementedError

            h, _, _, _, _ = model(**input_args)


            # loss_value = 0.0
            if len(h.shape) == 1:
                h = h.unsqueeze(0)
            loss_value = loss_fn(h=h, y=y_disc, t=event_time, c=censor)

            risk, risk_by_bin = _calculate_risk(h)
            all_risk_by_bin_scores.append(risk_by_bin)
            all_risk_scores, all_censorships, all_event_times, clinical_data_list = _update_arrays(all_risk_scores, all_censorships, all_event_times,all_clinical_data, event_time, censor, risk, clinical_data_list)
            all_logits.append(h.detach().cpu().numpy())
            total_loss += loss_value
            all_slide_ids.append(slide_ids.values[count])
            count += 1

    total_loss /= len(loader.dataset)
    all_risk_scores = np.concatenate(all_risk_scores, axis=0)
    all_risk_by_bin_scores = np.concatenate(all_risk_by_bin_scores, axis=0)
    all_censorships = np.concatenate(all_censorships, axis=0)
    all_event_times = np.concatenate(all_event_times, axis=0)
    all_logits = np.concatenate(all_logits, axis=0)
    
    patient_results = {}
    for i in range(len(all_slide_ids)):
        slide_id = slide_ids.values[i]
        case_id = slide_id[:12]
        patient_results[case_id] = {}
        patient_results[case_id]["time"] = all_event_times[i]
        patient_results[case_id]["risk"] = all_risk_scores[i]
        patient_results[case_id]["censorship"] = all_censorships[i]
        patient_results[case_id]["clinical"] = all_clinical_data[i]
        patient_results[case_id]["logits"] = all_logits[i]
    
    c_index, c_index2, BS, IBS, iauc = _calculate_metrics(loader, dataset_factory, survival_train, all_risk_scores, all_censorships, all_event_times, all_risk_by_bin_scores)

    return patient_results, c_index, c_index2, BS, IBS, iauc, total_loss

def _save_results(cur, results_dict, args):
    r"""
    Saves the results of the model.

    Args:
        - cur
        - results_dict
        - args: argspace.Namespace
    """
    filename = os.path.join(args.results_dir, "split_{}_results.pkl".format(cur))
    if os.path.exists(filename):
        os.remove(filename)
    print("Saving results...")
    _save_pkl(filename, results_dict)


def _step(cur, args, loss_fn, model, optimizer, train_loader, val_loader, log_file):
    r"""
    Trains the model for the set number of epochs and validates it.
    
    Args:
        - cur
        - args
        - loss_fn
        - model
        - optimizer
        - train_loader
        - val_loader
        - log_file
        
    Returns:
        - results_dict : dictionary
        - val_cindex : Float
        - val_cindex_ipcw  : Float
        - val_BS : List
        - val_IBS : Float
        - val_iauc : Float
        - total_loss : Float
    """



    all_survival = _extract_survival_metadata(train_loader, val_loader)
    
    for epoch in range(args.max_epochs):
        _train_loop_survival(args, epoch, model, args.omics_format, train_loader, optimizer, loss_fn, log_file)
        results_dict, val_cindex, val_cindex_ipcw, val_BS, val_IBS, val_iauc, total_loss = _summary(args.dataset_factory,
        model, args.omics_format, val_loader, loss_fn, all_survival,args.miss)
        print(
            'Epoch:{} Val c-index: {:.4f} | Final Val c-index2: {:.4f} | Final Val IBS: {:.4f} | Final Val iauc: {:.4f}'.format(
                epoch,
                val_cindex,
                val_cindex_ipcw,
                val_IBS,
                val_iauc
            ))
        log_file.write(
            'Epoch:{} Val c-index: {:.4f} | Final Val c-index2: {:.4f} | Final Val IBS: {:.4f} | Final Val iauc: {:.4f}\n'.format(
                epoch,
                val_cindex,
                val_cindex_ipcw,
                val_IBS,
                val_iauc
            ))
        if val_cindex >= args.max_cindex:
            args.max_cindex = val_cindex
            args.max_cindex_epoch = epoch
            torch.save(model.state_dict(), os.path.join(args.results_dir, "model_best_s{}.pth".format(cur)))
            _save_results(cur, results_dict, args)

    
    # save the trained model
    torch.save(model.state_dict(), os.path.join(args.results_dir, "s_{}_checkpoint.pth".format(cur)))
    
    results_dict, val_cindex, val_cindex_ipcw, val_BS, val_IBS, val_iauc, total_loss = _summary(args.dataset_factory, model, args.omics_format, val_loader, loss_fn, all_survival)
    
    print('Final Val c-index: {:.4f} | Final Val c-index2: {:.4f} | Final Val IBS: {:.4f} | Final Val iauc: {:.4f}'.format(
        val_cindex, 
        val_cindex_ipcw,
        val_IBS,
        val_iauc
        ))
    log_file.write('Final Val c-index: {:.4f} | Final Val c-index2: {:.4f} | Final Val IBS: {:.4f} | Final Val iauc: {:.4f}\n'.format(
        val_cindex,
        val_cindex_ipcw,
        val_IBS,
        val_iauc
        ))

    best_model = torch.load(os.path.join(args.results_dir, "model_best_s{}.pth".format(cur)))
    model.load_state_dict(best_model)
    _, val_cindex, val_cindex_ipcw, val_BS, val_IBS, val_iauc, total_loss = _summary(args.dataset_factory, model, args.omics_format, val_loader, loss_fn, all_survival)
    print('Best Val c-index: {:.4f} | Best Val c-index2: {:.4f} | Best Val IBS: {:.4f} | Best Val iauc: {:.4f}'.format(
        val_cindex,
        val_cindex_ipcw,
        val_IBS,
        val_iauc
    ))
    log_file.write('Best Val c-index: {:.4f} | Best Val c-index2: {:.4f} | Best Val IBS: {:.4f} | Best Val iauc: {:.4f}\n'.format(
        val_cindex,
        val_cindex_ipcw,
        val_IBS,
        val_iauc
    ))


    return results_dict, (args.max_cindex, val_cindex_ipcw, val_BS, val_IBS, val_iauc, total_loss)

def _train_val(datasets, cur, args, log_file):
    """   
    Performs train val test for the fold over number of epochs

    Args:
        - datasets : tuple
        - cur : Int 
        - args : argspace.Namespace
        - log_file : file
    
    Returns:
        - results_dict : dict
        - val_cindex : Float
        - val_cindex2 : Float
        - val_BS : Float
        - val_IBS : Float
        - val_iauc : Float
        - total_loss : Float
    """

    #----> gets splits and summarize
    train_split, val_split = _get_splits(datasets, cur, args)
    
    #----> init loss function
    loss_fn = _init_loss_function(args)

    #----> init model
    model = _init_model(args)
    
    #---> init optimizer
    optimizer = _init_optim(args, model)
    
    #---> init loaders
    train_loader, val_loader = _init_loaders(args, train_split, val_split)

    #---> do train val
    results_dict, (val_cindex, val_cindex2, val_BS, val_IBS, val_iauc, total_loss) = _step(cur, args, loss_fn, model, optimizer, train_loader, val_loader, log_file)

    return results_dict, (val_cindex, val_cindex2, val_BS, val_IBS, val_iauc, total_loss)