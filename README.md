# Lilypad Quickstart
This quickstart repo uses [Pulumi](https://www.pulumi.com/) to create AWS resources to build a Lilypad Network instance.

## Setup
In order to set the required secrets for an Ethereum wallet, export a private key from MetaMask
and generate an Alchemy API key, and run the commands below to add them to the Pulumi config: 
```
pulumi config set --secret Web3PrivateKey
pulumi config set --secret AlchemyAPIKey
```
