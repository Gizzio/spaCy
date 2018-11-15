# coding: utf8
from __future__ import unicode_literals, division, print_function

import plac
from pathlib import Path
import tqdm
from thinc.neural._classes.model import Model
from timeit import default_timer as timer
import json
import shutil

from ._messages import Messages
from .._ml import create_default_optimizer
from ..attrs import PROB, IS_OOV, CLUSTER, LANG
from ..gold import GoldCorpus
from ..util import prints, minibatch, minibatch_by_words, write_json
from .. import util
from .. import about


# Take dropout and batch size as generators of values -- dropout
# starts high and decays sharply, to force the optimizer to explore.
# Batch size starts at 1 and grows, so that we make updates quickly
# at the beginning of training.
dropout_rates = util.decaying(util.env_opt('dropout_from', 0.2),
                              util.env_opt('dropout_to', 0.2),
                              util.env_opt('dropout_decay', 0.0))
batch_sizes = util.compounding(util.env_opt('batch_from', 1000),
                               util.env_opt('batch_to', 1000),
                               util.env_opt('batch_compound', 1.001))


@plac.annotations(
    lang=("model language", "positional", None, str),
    output_path=("output directory to store model in", "positional", None, Path),
    train_path=("location of JSON-formatted training data", "positional", None, Path),
    dev_path=("location of JSON-formatted development data", "positional", None, Path),
    base_model=("name of model to update (optional)", "option", "b", str),
    pipeline=("Comma-separated names of pipeline components to train", "option", "p", str),
    vectors=("Model to load vectors from", "option", "v", str),
    n_iter=("number of iterations", "option", "n", int),
    n_examples=("number of examples", "option", "ns", int),
    use_gpu=("Use GPU", "option", "g", int),
    version=("Model version", "option", "V", str),
    meta_path=("Optional path to meta.json. All relevant properties will be overwritten.", "option", "m", Path),
    parser_multitasks=("Side objectives for parser CNN, e.g. dep dep,tag", "option", "pt", str),
    entity_multitasks=("Side objectives for ner CNN, e.g. dep dep,tag", "option", "et", str),
    noise_level=("Amount of corruption to add for data augmentation", "option", "nl", float),
    gold_preproc=("Use gold preprocessing", "flag", "G", bool),
    learn_tokens=("Make parser learn gold-standard tokenization", "flag", "T", bool),
    verbose=("Display more information for debug", "flag", "vv", bool),
    debug=("Run data diagnostics before training", "flag", "D", bool)
)
def train(lang, output_path, train_path, dev_path, base_model=None,
          pipeline='tagger,parser,ner', vectors=None, n_iter=30, n_examples=0,
          use_gpu=-1, version="0.0.0", meta_path=None, parser_multitasks='',
          entity_multitasks='', noise_level=0.0, gold_preproc=False,
          learn_tokens=False, verbose=False, debug=False):

    util.fix_random_seed()
    util.set_env_log(verbose)

    # Make sure all files and paths exists if they are needed
    if not train_path.exists():
        prints(train_path, title=Messages.M050, exits=1)
    if not dev_path.exists():
        prints(dev_path, title=Messages.M051, exits=1)
    if meta_path is not None and not meta_path.exists():
        prints(meta_path, title=Messages.M020, exits=1)
    meta = util.read_json(meta_path) if meta_path else {}
    if not isinstance(meta, dict):
        prints(Messages.M053.format(meta_type=type(meta)),
               title=Messages.M052, exits=1)
    if not output_path.exists():
        output_path.mkdir()

    # Set up the base model and pipeline. If a base model is specified, load
    # the model and make sure the pipeline matches the pipeline setting. If
    # training starts from a blank model, intitalize the language class.
    pipeline = [p.strip() for p in pipeline.split(',')]
    print("Training pipeline: {}".format(pipeline))
    if base_model:
        print("Starting with base model '{}'".format(base_model))
        nlp = util.load_model(base_model)
        other_pipes = [pipe for pipe in nlp.pipe_names if pipe not in pipeline]
        nlp.disable_pipes(*other_pipes)
        for pipe in pipeline:
            if pipe not in nlp.pipe_names:
                nlp.add_pipe(nlp.create_pipe(pipe))
    else:
        print("Starting with blank model '{}'".format(lang))
        lang_cls = util.get_lang_class(lang)
        nlp = lang_cls()
        for pipe in pipeline:
            nlp.add_pipe(nlp.create_pipe(pipe))

    if learn_tokens:
        nlp.add_pipe(nlp.create_pipe('merge_subtokens'))

    if vectors:
        _load_vectors(nlp, vectors)

    # Multitask objectives
    for pipe_name, multitasks in [('parser', parser_multitasks),
                                  ('ner', entity_multitasks)]:
        if multitasks:
            if not pipe_name in pipeline:
                raise ValueError("Needs '{}' in pipeline".format(pipe_name))
            pipe = nlp.get_pipe(pipe_name)
            for objective in multitasks.split(','):
                pipe.add_multitask_objective(objective)

    # Prepare training corpus
    print("Counting training words (limit={})".format(n_examples))
    corpus = GoldCorpus(train_path, dev_path, limit=n_examples)
    n_train_words = corpus.count_train()

    if base_model:
        # Start with an existing model, use default optimizer
        optimizer = create_default_optimizer(Model.ops)
    else:
        # Start with a blank model, call begin_training
        optimizer = nlp.begin_training(lambda: corpus.train_tuples, device=use_gpu)
    nlp._optimizer = None

    print("\nItn.  Dep Loss  NER Loss  UAS     NER P.  NER R.  NER F.  Tag %   Token %  CPU WPS  GPU WPS")
    try:
        for i in range(n_iter):
            train_docs = corpus.train_docs(nlp, noise_level=noise_level,
                                           gold_preproc=gold_preproc, max_length=0)
            words_seen = 0
            with tqdm.tqdm(total=n_train_words, leave=False) as pbar:
                losses = {}
                for batch in minibatch_by_words(train_docs, size=batch_sizes):
                    if not batch:
                        continue
                    docs, golds = zip(*batch)
                    nlp.update(docs, golds, sgd=optimizer,
                               drop=next(dropout_rates), losses=losses)
                    pbar.update(sum(len(doc) for doc in docs))
                    words_seen += sum(len(doc) for doc in docs)
            with nlp.use_params(optimizer.averages):
                util.set_env_log(False)
                epoch_model_path = output_path / ('model%d' % i)
                nlp.to_disk(epoch_model_path)
                nlp_loaded = util.load_model_from_path(epoch_model_path)
                dev_docs = list(corpus.dev_docs(
                                nlp_loaded,
                                gold_preproc=gold_preproc))
                nwords = sum(len(doc_gold[0]) for doc_gold in dev_docs)
                start_time = timer()
                scorer = nlp_loaded.evaluate(dev_docs, debug)
                end_time = timer()
                if use_gpu < 0:
                    gpu_wps = None
                    cpu_wps = nwords/(end_time-start_time)
                else:
                    gpu_wps = nwords/(end_time-start_time)
                    with Model.use_device('cpu'):
                        nlp_loaded = util.load_model_from_path(epoch_model_path)
                        dev_docs = list(corpus.dev_docs(
                                        nlp_loaded, gold_preproc=gold_preproc))
                        start_time = timer()
                        scorer = nlp_loaded.evaluate(dev_docs)
                        end_time = timer()
                        cpu_wps = nwords/(end_time-start_time)
                acc_loc = output_path / ('model%d' % i) / 'accuracy.json'
                write_json(acc_loc, scorer.scores)

                # Update model meta.json
                meta['lang'] = nlp.lang
                meta['pipeline'] = nlp.pipe_names
                meta['spacy_version'] = '>=%s' % about.__version__
                meta['accuracy'] = scorer.scores
                meta['speed'] = {'nwords': nwords, 'cpu': cpu_wps,
                                 'gpu': gpu_wps}
                meta['vectors'] = {'width': nlp.vocab.vectors_length,
                                   'vectors': len(nlp.vocab.vectors),
                                   'keys': nlp.vocab.vectors.n_keys}
                meta.setdefault('name', 'model%d' % i)
                meta.setdefault('version', version)
                meta_loc = output_path / ('model%d' % i) / 'meta.json'
                write_json(meta_loc, meta)

                util.set_env_log(verbose)

            print_progress(i, losses, scorer.scores, cpu_wps=cpu_wps,
                           gpu_wps=gpu_wps)
    finally:
        print("\nSaving model...")
        with nlp.use_params(optimizer.averages):
            final_model_path = output_path / 'model-final'
            nlp.to_disk(final_model_path)
            print(final_model_path)

    _collate_best_model(meta, output_path, nlp.pipe_names)


def _load_vectors(nlp, vectors):
    print("Load vectors model", vectors)
    util.load_model(vectors, vocab=nlp.vocab)
    for lex in nlp.vocab:
        values = {}
        for attr, func in nlp.vocab.lex_attr_getters.items():
            # These attrs are expected to be set by data. Others should
            # be set by calling the language functions.
            if attr not in (CLUSTER, PROB, IS_OOV, LANG):
                values[lex.vocab.strings[attr]] = func(lex.orth_)
        lex.set_attrs(**values)
        lex.is_oov = False


def _collate_best_model(meta, output_path, components):
    bests = {}
    for component in components:
        bests[component] = _find_best(output_path, component)
    best_dest = output_path / 'model-best'
    shutil.copytree(output_path / 'model-final', best_dest)
    for component, best_component_src in bests.items():
        shutil.rmtree(best_dest / component)
        shutil.copytree(best_component_src / component, best_dest / component)
        with (best_component_src / 'accuracy.json').open() as file_:
            accs = json.load(file_)
        for metric in _get_metrics(component):
            meta['accuracy'][metric] = accs[metric]
    write_json(best_dest / 'meta.json', meta)


def _find_best(experiment_dir, component):
    accuracies = []
    for epoch_model in experiment_dir.iterdir():
        if epoch_model.is_dir() and epoch_model.parts[-1] != "model-final":
            accs = json.load((epoch_model / "accuracy.json").open())
            scores = [accs.get(metric, 0.0) for metric in _get_metrics(component)]
            accuracies.append((scores, epoch_model))
    if accuracies:
        return max(accuracies)[1]
    else:
        return None


def _get_metrics(component):
    if component == "parser":
        return ("las", "uas", "token_acc")
    elif component == "tagger":
        return ("tags_acc",)
    elif component == "ner":
        return ("ents_f", "ents_p", "ents_r")
    return ("token_acc",)


def print_progress(itn, losses, dev_scores, cpu_wps=0.0, gpu_wps=0.0):
    scores = {}
    for col in ['dep_loss', 'tag_loss', 'uas', 'tags_acc', 'token_acc',
                'ents_p', 'ents_r', 'ents_f', 'cpu_wps', 'gpu_wps']:
        scores[col] = 0.0
    scores['dep_loss'] = losses.get('parser', 0.0)
    scores['ner_loss'] = losses.get('ner', 0.0)
    scores['tag_loss'] = losses.get('tagger', 0.0)
    scores.update(dev_scores)
    scores['cpu_wps'] = cpu_wps
    scores['gpu_wps'] = gpu_wps or 0.0
    tpl = ''.join((
        '{:<6d}',
        '{dep_loss:<10.3f}',
        '{ner_loss:<10.3f}',
        '{uas:<8.3f}',
        '{ents_p:<8.3f}',
        '{ents_r:<8.3f}',
        '{ents_f:<8.3f}',
        '{tags_acc:<8.3f}',
        '{token_acc:<9.3f}',
        '{cpu_wps:<9.1f}',
        '{gpu_wps:.1f}',
    ))
    print(tpl.format(itn, **scores))
